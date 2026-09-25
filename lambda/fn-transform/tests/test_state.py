"""The duplicate-delivery claim and the current-state snapshot."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import pytest
from botocore.exceptions import ClientError
from config import ObjectRef
from exceptions import ObjectInFlightError
from state import (
    CLAIM_STALE_AFTER_SECONDS,
    CLAIMED_AT_EPOCH_ATTRIBUTE,
    SECONDS_PER_DAY,
    STATUS_ATTRIBUTE,
    TTL_ATTRIBUTE,
    ClaimOutcome,
    ClaimStatus,
    claim_key,
    claim_object,
    complete_claim,
    release_claim,
    upsert_snapshot,
)

from conftest import STATE_TABLE, Stack

if TYPE_CHECKING:
    from mypy_boto3_dynamodb.client import DynamoDBClient

NOW = datetime(2026, 6, 25, 10, 5, 41, tzinfo=UTC)
REF = ObjectRef(
    bucket="obu-poc-raw",
    key="raw/bms/environmental/2026/06/25/nhhb-env.csv",
    version_id="v1",
)


def take(stack: Stack, *, ref: ObjectRef = REF, now: datetime = NOW) -> ClaimOutcome:
    """Take the claim the way the transform does."""
    return claim_object(stack.dynamodb, STATE_TABLE, ref=ref, ttl_days=14, now=now)


def test_claim_object_is_taken_for_a_first_delivery(stack: Stack) -> None:
    """Nothing holds the claim, so this invocation takes it and does the work."""
    assert take(stack) is ClaimOutcome.TAKEN


def test_claim_object_is_already_done_for_a_delivery_after_completion(
    stack: Stack,
) -> None:
    """Only a completed object suppresses a later delivery."""
    take(stack)
    complete_claim(stack.dynamodb, STATE_TABLE, ref=REF, now=NOW)
    assert take(stack) is ClaimOutcome.ALREADY_DONE


def test_claim_object_raises_while_another_invocation_holds_a_fresh_claim(
    stack: Stack,
) -> None:
    """Giving way costs a retry; assuming success would drop the delivery."""
    take(stack)
    with pytest.raises(ObjectInFlightError, match=REF.key):
        take(stack)


def test_claim_object_takes_over_a_claim_whose_owner_never_finished(
    stack: Stack,
) -> None:
    """An invocation killed mid-flight must not hold the object forever."""
    take(stack)
    later = datetime.fromtimestamp(
        NOW.timestamp() + CLAIM_STALE_AFTER_SECONDS + 1, tz=UTC
    )
    assert take(stack, now=later) is ClaimOutcome.TAKEN


def test_claim_object_leaves_a_fresh_claim_alone_just_below_the_cutoff(
    stack: Stack,
) -> None:
    """The takeover window starts after the function could still be running."""
    take(stack)
    later = datetime.fromtimestamp(
        NOW.timestamp() + CLAIM_STALE_AFTER_SECONDS - 1, tz=UTC
    )
    with pytest.raises(ObjectInFlightError):
        take(stack, now=later)


def test_claim_object_records_the_claim_as_processing(stack: Stack) -> None:
    """The claim only counts as done once the object has been processed."""
    take(stack)
    item = _claim_item(stack.dynamodb)
    assert item[STATUS_ATTRIBUTE]["S"] == ClaimStatus.PROCESSING.value
    assert int(item[CLAIMED_AT_EPOCH_ATTRIBUTE]["N"]) == int(NOW.timestamp())


def test_complete_claim_marks_the_object_done(stack: Stack) -> None:
    """This is the write that makes a later delivery a no-op."""
    take(stack)
    assert complete_claim(stack.dynamodb, STATE_TABLE, ref=REF, now=NOW) is True
    assert _claim_item(stack.dynamodb)[STATUS_ATTRIBUTE]["S"] == ClaimStatus.DONE.value


def test_complete_claim_reports_a_claim_that_was_taken_over(stack: Stack) -> None:
    """Completing a claim someone else now owns is reported, not raised."""
    take(stack)
    complete_claim(stack.dynamodb, STATE_TABLE, ref=REF, now=NOW)
    assert complete_claim(stack.dynamodb, STATE_TABLE, ref=REF, now=NOW) is False


def test_release_claim_lets_the_next_delivery_take_the_object(stack: Stack) -> None:
    """A failed invocation gives the object back so the retry re-processes it."""
    take(stack)
    assert release_claim(stack.dynamodb, STATE_TABLE, ref=REF) is True
    assert take(stack) is ClaimOutcome.TAKEN


def test_release_claim_reports_failure_rather_than_raising(stack: Stack) -> None:
    """The caller is already handling a failure and must not be handed another."""
    assert release_claim(stack.dynamodb, "no-such-table", ref=REF) is False


def test_claim_object_treats_a_new_version_as_a_new_object(stack: Stack) -> None:
    """A re-drop under the same key is a different version and is processed."""
    take(stack)
    second = ObjectRef(bucket=REF.bucket, key=REF.key, version_id="v2")
    assert take(stack, ref=second) is ClaimOutcome.TAKEN


def test_claim_object_stamps_the_expiry_in_epoch_seconds(stack: Stack) -> None:
    """Milliseconds here would leave every claim far in the future and never swept."""
    take(stack)
    item = _claim_item(stack.dynamodb)
    assert int(item[TTL_ATTRIBUTE]["N"]) == int(NOW.timestamp()) + 14 * SECONDS_PER_DAY


def test_claim_object_reraises_an_error_that_is_not_the_condition_failing(
    stack: Stack,
) -> None:
    """A missing table is a platform failure and must not read as a duplicate."""
    with pytest.raises(ClientError):
        claim_object(stack.dynamodb, "no-such-table", ref=REF, ttl_days=14, now=NOW)


def test_upsert_snapshot_writes_one_current_state_item_for_the_entity(
    stack: Stack,
) -> None:
    """This item is what the read API serves, so the latest values land on it."""
    upsert_snapshot(
        stack.dynamodb,
        STATE_TABLE,
        entity_pk="room#NHHB-2.14",
        entity_type="room",
        values={"temperature_c": Decimal("26.2"), "humidity_pct": Decimal(55)},
        source_file=REF.key,
        now=NOW,
    )
    item = _snapshot_item(stack.dynamodb)
    assert item["temperature_c"]["N"] == "26.2"
    assert item["entity_type"]["S"] == "room"
    assert item["updated_at"]["S"] == NOW.isoformat()
    assert item["source_file"]["S"] == REF.key


def test_upsert_snapshot_replaces_the_previous_reading(stack: Stack) -> None:
    """The snapshot is current state, not a history of readings."""
    for temperature in ("26.2", "24.8"):
        upsert_snapshot(
            stack.dynamodb,
            STATE_TABLE,
            entity_pk="room#NHHB-2.14",
            entity_type="room",
            values={"temperature_c": Decimal(temperature)},
            source_file=REF.key,
            now=NOW,
        )
    assert _snapshot_item(stack.dynamodb)["temperature_c"]["N"] == "24.8"


def test_upsert_snapshot_leaves_an_absent_value_off_the_item(stack: Stack) -> None:
    """A field with no reading is absent rather than served as a null."""
    upsert_snapshot(
        stack.dynamodb,
        STATE_TABLE,
        entity_pk="room#NHHB-2.14",
        entity_type="room",
        values={"temperature_c": Decimal("26.2"), "humidity_pct": None},
        source_file=REF.key,
        now=NOW,
    )
    assert "humidity_pct" not in _snapshot_item(stack.dynamodb)


def _claim_item(client: DynamoDBClient) -> dict[str, Any]:
    response = client.get_item(
        TableName=STATE_TABLE,
        Key={"pk": {"S": claim_key(REF)}, "sk": {"S": "claim"}},
    )
    return dict(response["Item"])


def _snapshot_item(client: DynamoDBClient) -> dict[str, Any]:
    response = client.get_item(
        TableName=STATE_TABLE,
        Key={"pk": {"S": "room#NHHB-2.14"}, "sk": {"S": "snapshot"}},
    )
    return dict(response["Item"])
