"""State-table writes: the duplicate-delivery claim and the snapshot upsert."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from botocore.exceptions import BotoCoreError, ClientError
from exceptions import ObjectInFlightError

if TYPE_CHECKING:
    from collections.abc import Mapping

    from config import ObjectRef
    from mypy_boto3_dynamodb.client import DynamoDBClient

CLAIM_SORT_KEY = "claim"
SNAPSHOT_SORT_KEY = "snapshot"
TTL_ATTRIBUTE = "ttl"
"""Attribute the state table's time-to-live is configured on."""

STATUS_ATTRIBUTE = "status"
CLAIMED_AT_EPOCH_ATTRIBUTE = "claimed_at_epoch"

SECONDS_PER_DAY = 86_400
CONDITION_FAILED = "ConditionalCheckFailedException"

CLAIM_STALE_AFTER_SECONDS = 360
"""When an in-flight claim may be taken over by a later delivery.

The function timeout in ``required_event_source_config.json`` is 300 seconds,
plus a 60 second margin. A claim older than this whose owner never completed
belongs to an invocation that was hard-killed: a timeout that got past the
remaining-time guard, an out-of-memory kill, or the execution environment being
torn down. Nothing will ever mark it done, so the next delivery takes it.
"""


class ClaimStatus(StrEnum):
    """Where the claim on an object stands."""

    PROCESSING = "processing"
    DONE = "done"


class ClaimOutcome(StrEnum):
    """What taking the claim on an object produced."""

    TAKEN = "TAKEN"
    """This invocation owns the claim and must complete or release it."""

    ALREADY_DONE = "ALREADY_DONE"
    """The object was processed to completion by an earlier delivery."""


def claim_key(ref: ObjectRef) -> str:
    """Partition key of the claim item for one object version."""
    return f"claim#{ref.bucket}#{ref.key}#{ref.claim_version}"


def claim_object(
    client: DynamoDBClient,
    table_name: str,
    *,
    ref: ObjectRef,
    ttl_days: int,
    now: datetime,
) -> ClaimOutcome:
    """Take the claim on an object, so a repeat delivery does no work twice.

    The claim is two-phase. Taking it writes ``processing``; finishing the
    object marks it ``done``. Only ``done`` suppresses a later delivery, so an
    invocation that fails part way releases its claim and the retry re-processes
    the object from the start.

    The write is conditional on the claim not existing, or on an existing
    in-flight claim being older than ``CLAIM_STALE_AFTER_SECONDS``. That
    condition is what makes two concurrent deliveries of the same object version
    safe, and what stops a hard-killed invocation holding the object forever.

    Args:
        client: DynamoDB client.
        table_name: State table name.
        ref: The delivered object.
        ttl_days: How long the claim is kept before the table expires it.
        now: Invocation timestamp the claim is stamped and expired from.

    Returns:
        ``TAKEN`` when this invocation owns the claim, ``ALREADY_DONE`` when an
        earlier delivery finished the object.

    Raises:
        ObjectInFlightError: Another invocation holds a claim on this exact
            object version and started recently enough to still be running.
            Raising sends the delivery back through Lambda's retry, and an
            object that stays stuck reaches the dead-letter queue where a person
            sees it. A genuine concurrent duplicate of a long-running file
            therefore costs one wasted retry, which is accepted: the
            alternative is silently dropping a delivery that may be the only
            one left.
    """
    if _put_claim(client, table_name, ref=ref, ttl_days=ttl_days, now=now):
        return ClaimOutcome.TAKEN

    held = _read_claim(client, table_name, ref)
    if held is None:
        # The claim expired between the write and the read. One more attempt,
        # then treat it as held rather than racing indefinitely.
        if _put_claim(client, table_name, ref=ref, ttl_days=ttl_days, now=now):
            return ClaimOutcome.TAKEN
        raise ObjectInFlightError(_in_flight_message(ref), unit=_unit(ref))

    if held.get(STATUS_ATTRIBUTE, {}).get("S") == ClaimStatus.DONE.value:
        return ClaimOutcome.ALREADY_DONE

    raise ObjectInFlightError(_in_flight_message(ref), unit=_unit(ref))


def complete_claim(
    client: DynamoDBClient,
    table_name: str,
    *,
    ref: ObjectRef,
    now: datetime,
) -> bool:
    """Mark the object done, so later deliveries of it do nothing.

    Args:
        client: DynamoDB client.
        table_name: State table name.
        ref: The delivered object.
        now: Invocation timestamp stamped as the completion time.

    Returns:
        True when the claim was marked done. False when the claim was no longer
        this invocation's to complete, which happens only after another
        delivery took over a claim presumed dead.
    """
    try:
        client.update_item(
            TableName=table_name,
            Key={"pk": {"S": claim_key(ref)}, "sk": {"S": CLAIM_SORT_KEY}},
            UpdateExpression="SET #status = :done, completed_at = :now",
            ConditionExpression="#status = :processing",
            ExpressionAttributeNames={"#status": STATUS_ATTRIBUTE},
            ExpressionAttributeValues={
                ":done": {"S": ClaimStatus.DONE.value},
                ":processing": {"S": ClaimStatus.PROCESSING.value},
                ":now": {"S": now.isoformat()},
            },
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] == CONDITION_FAILED:
            return False
        raise
    return True


def release_claim(
    client: DynamoDBClient,
    table_name: str,
    *,
    ref: ObjectRef,
) -> bool:
    """Give up the claim so the retry of a failed invocation can re-process.

    Args:
        client: DynamoDB client.
        table_name: State table name.
        ref: The delivered object.

    Returns:
        True when the claim was deleted, False when the delete itself failed.
        The caller is already handling a failure, so this never raises: a claim
        left behind is taken over once it goes stale.
    """
    try:
        client.delete_item(
            TableName=table_name,
            Key={"pk": {"S": claim_key(ref)}, "sk": {"S": CLAIM_SORT_KEY}},
            ConditionExpression="#status = :processing",
            ExpressionAttributeNames={"#status": STATUS_ATTRIBUTE},
            ExpressionAttributeValues={
                ":processing": {"S": ClaimStatus.PROCESSING.value}
            },
        )
    except BotoCoreError, ClientError:
        return False
    return True


def _put_claim(
    client: DynamoDBClient,
    table_name: str,
    *,
    ref: ObjectRef,
    ttl_days: int,
    now: datetime,
) -> bool:
    """Write an in-flight claim, taking over one that has gone stale."""
    now_epoch = int(now.timestamp())
    try:
        client.put_item(
            TableName=table_name,
            Item={
                "pk": {"S": claim_key(ref)},
                "sk": {"S": CLAIM_SORT_KEY},
                STATUS_ATTRIBUTE: {"S": ClaimStatus.PROCESSING.value},
                "claimed_at": {"S": now.isoformat()},
                CLAIMED_AT_EPOCH_ATTRIBUTE: {"N": str(now_epoch)},
                TTL_ATTRIBUTE: {"N": str(now_epoch + ttl_days * SECONDS_PER_DAY)},
            },
            ConditionExpression=(
                "attribute_not_exists(pk) OR "
                "(#status = :processing AND claimed_at_epoch < :stale_before)"
            ),
            ExpressionAttributeNames={"#status": STATUS_ATTRIBUTE},
            ExpressionAttributeValues={
                ":processing": {"S": ClaimStatus.PROCESSING.value},
                ":stale_before": {"N": str(now_epoch - CLAIM_STALE_AFTER_SECONDS)},
            },
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] == CONDITION_FAILED:
            return False
        raise
    return True


def _read_claim(
    client: DynamoDBClient,
    table_name: str,
    ref: ObjectRef,
) -> dict[str, Any] | None:
    """Read the claim that beat this invocation to the object."""
    response = client.get_item(
        TableName=table_name,
        Key={"pk": {"S": claim_key(ref)}, "sk": {"S": CLAIM_SORT_KEY}},
        ConsistentRead=True,
    )
    if "Item" not in response:
        return None
    return dict(response["Item"])


def _in_flight_message(ref: ObjectRef) -> str:
    return f"s3://{ref.bucket}/{ref.key} is being processed by another invocation"


def _unit(ref: ObjectRef) -> dict[str, object]:
    return {"bucket": ref.bucket, "object_key": ref.key}


def upsert_snapshot(  # noqa: PLR0913 - the key, the values, the provenance and the stamp are one write
    client: DynamoDBClient,
    table_name: str,
    *,
    entity_pk: str,
    entity_type: str,
    values: Mapping[str, object],
    source_file: str,
    now: datetime,
) -> None:
    """Write the current-state item for one entity.

    Args:
        client: DynamoDB client.
        table_name: State table name.
        entity_pk: ``{entity type}#{entity value}`` partition key.
        entity_type: The mapping config's entity type, written as its own
            attribute so the entity-type GSI can list every entity of one
            type without needing the value half of the key in advance.
        values: The entity's latest mapped values.
        source_file: Key of the object the values came from.
        now: Invocation timestamp stamped as the update time.
    """
    item: dict[str, Any] = {
        "pk": {"S": entity_pk},
        "sk": {"S": SNAPSHOT_SORT_KEY},
        "entity_type": {"S": entity_type},
        "updated_at": {"S": now.isoformat()},
        "source_file": {"S": source_file},
    }
    for name, value in values.items():
        attribute = to_attribute(value)
        if attribute is not None:
            item[name] = attribute
    client.put_item(TableName=table_name, Item=item)


def to_attribute(value: object) -> dict[str, Any] | None:
    """Turn one mapped value into a DynamoDB attribute.

    Returns None for an absent value, which is then left off the item rather
    than written as a null, so the read API sees only fields that have a
    reading.
    """
    match value:
        case None:
            return None
        case bool():
            return {"BOOL": value}
        case Decimal() | int():
            return {"N": str(value)}
        case datetime():
            return {"S": value.isoformat()}
        case _:
            return {"S": str(value)}
