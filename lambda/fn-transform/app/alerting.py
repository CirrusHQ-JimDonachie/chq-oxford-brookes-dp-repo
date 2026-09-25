"""Threshold rules, the per-entity alert state machine and the breach event.

Alerting is transition-only in both directions. A room that stays over its
threshold produces one event, not one per reading, and the recovery direction
emits an event only when the rule asks for it.
"""

from __future__ import annotations

import json
import operator
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from botocore.exceptions import ClientError
from state import CONDITION_FAILED

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from datetime import datetime

    from mypy_boto3_dynamodb.client import DynamoDBClient
    from mypy_boto3_events.client import EventBridgeClient

ALERT_SORT_KEY_PREFIX = "alert#"
RULE_PARTITION_PREFIX = "rule#"

BREACH_DETAIL_TYPE = "Threshold Breach"
RECOVERY_DETAIL_TYPE = "Threshold Recovery"

COMPARISONS: dict[str, Callable[[Decimal, Decimal], bool]] = {
    ">": operator.gt,
    ">=": operator.ge,
    "<": operator.lt,
    "<=": operator.le,
    "==": operator.eq,
    "!=": operator.ne,
}


class AlertState(StrEnum):
    """Where an entity and metric currently sit against their rule."""

    OK = "OK"
    BREACH = "BREACH"


KNOWN_STATES = frozenset(state.value for state in AlertState)


class Transition(StrEnum):
    """The state change a reading caused, when it caused one."""

    TO_BREACH = "TO_BREACH"
    TO_OK = "TO_OK"


@dataclass(frozen=True)
class AlertRule:
    """One threshold rule for an entity and metric."""

    entity_pk: str
    metric: str
    comparison: str
    threshold: Decimal
    severity: str
    notify_on_recovery: bool


@dataclass(frozen=True)
class TransitionOutcome:
    """A state change that was written, and when its breach began."""

    transition: Transition
    breach_started_at: str | None


def load_rules(
    client: DynamoDBClient,
    table_name: str,
    entity_pk: str,
) -> list[AlertRule]:
    """Read every threshold rule configured for one entity.

    Rules live beside the mapping configs in the config table, keyed
    ``rule#{entity type}#{entity value}`` with the metric name as the sort key.

    Args:
        client: DynamoDB client.
        table_name: Config table name.
        entity_pk: ``{entity type}#{entity value}`` the rules apply to.

    Returns:
        The entity's rules. A rule whose comparison or threshold cannot be read
        is skipped, so one bad rule row does not stop the rest of the file.
    """
    paginator = client.get_paginator("query")
    rules: list[AlertRule] = []
    for page in paginator.paginate(
        TableName=table_name,
        KeyConditionExpression="pk = :pk",
        ExpressionAttributeValues={":pk": {"S": f"{RULE_PARTITION_PREFIX}{entity_pk}"}},
    ):
        for item in page.get("Items", []):
            rule = _parse_rule(entity_pk, item)
            if rule is not None:
                rules.append(rule)
    return rules


def _parse_rule(entity_pk: str, item: Mapping[str, Any]) -> AlertRule | None:
    metric = item.get("sk", {}).get("S")
    comparison = item.get("comparison", {}).get("S")
    raw_threshold = item.get("threshold", {}).get("N")
    if not metric or comparison not in COMPARISONS or raw_threshold is None:
        return None
    try:
        threshold = Decimal(raw_threshold)
    except InvalidOperation:
        return None
    return AlertRule(
        entity_pk=entity_pk,
        metric=metric,
        comparison=comparison,
        threshold=threshold,
        severity=item.get("severity", {}).get("S") or "unspecified",
        notify_on_recovery=bool(item.get("notify_on_recovery", {}).get("BOOL", False)),
    )


def load_alert_states(
    client: DynamoDBClient,
    table_name: str,
    entity_pk: str,
) -> dict[str, AlertState]:
    """Read the current alert state for every metric of one entity.

    Args:
        client: DynamoDB client.
        table_name: State table name.
        entity_pk: ``{entity type}#{entity value}``.

    Returns:
        Metric name to state. A metric with no item yet is absent, which the
        caller treats as OK.
    """
    paginator = client.get_paginator("query")
    states: dict[str, AlertState] = {}
    for page in paginator.paginate(
        TableName=table_name,
        KeyConditionExpression="pk = :pk AND begins_with(sk, :prefix)",
        ExpressionAttributeValues={
            ":pk": {"S": entity_pk},
            ":prefix": {"S": ALERT_SORT_KEY_PREFIX},
        },
    ):
        for item in page.get("Items", []):
            sort_key = item.get("sk", {}).get("S", "")
            raw_state = item.get("state", {}).get("S", "")
            if sort_key.startswith(ALERT_SORT_KEY_PREFIX) and raw_state in KNOWN_STATES:
                states[sort_key.removeprefix(ALERT_SORT_KEY_PREFIX)] = AlertState(
                    raw_state
                )
    return states


def breaches(value: object, rule: AlertRule) -> bool | None:
    """Test one reading against one rule.

    Args:
        value: The mapped value for the rule's metric.
        rule: The rule to apply.

    Returns:
        Whether the reading breaches, or None when the value is absent or is
        not a number the rule can be applied to.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal | int):
        return COMPARISONS[rule.comparison](Decimal(value), rule.threshold)
    return None


def apply_transition(
    client: DynamoDBClient,
    table_name: str,
    *,
    rule: AlertRule,
    is_breaching: bool,
    now: datetime,
) -> TransitionOutcome | None:
    """Move an entity and metric between OK and BREACH, once.

    The write is conditional on the state the transition moves away from. Two
    near-simultaneous readings therefore produce one transition: the loser's
    condition fails, which is the correct silent outcome, not an error.

    Args:
        client: DynamoDB client.
        table_name: State table name.
        rule: The rule whose state is being moved.
        is_breaching: Whether the latest reading breaches the rule.
        now: Invocation timestamp stamped on the item.

    Returns:
        The transition that was written, or None when the state was already
        where the reading puts it or another writer got there first.
    """
    stamped = now.isoformat()
    values: dict[str, Any] = {
        ":ok": {"S": AlertState.OK.value},
        ":breach": {"S": AlertState.BREACH.value},
        ":now": {"S": stamped},
    }
    if is_breaching:
        update = (
            "SET #state = :breach, breach_started_at = :now, updated_at = :now, "
            "threshold = :threshold, severity = :severity"
        )
        condition = "attribute_not_exists(pk) OR #state = :ok"
        values[":threshold"] = {"N": str(rule.threshold)}
        values[":severity"] = {"S": rule.severity}
    else:
        update = "SET #state = :ok, updated_at = :now REMOVE breach_started_at"
        condition = "#state = :breach"

    try:
        response = client.update_item(
            TableName=table_name,
            Key={
                "pk": {"S": rule.entity_pk},
                "sk": {"S": f"{ALERT_SORT_KEY_PREFIX}{rule.metric}"},
            },
            UpdateExpression=update,
            ConditionExpression=condition,
            ExpressionAttributeNames={"#state": "state"},
            ExpressionAttributeValues=values,
            ReturnValues="ALL_OLD",
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] == CONDITION_FAILED:
            return None
        raise

    if is_breaching:
        return TransitionOutcome(
            transition=Transition.TO_BREACH, breach_started_at=stamped
        )
    previous = response.get("Attributes", {}).get("breach_started_at", {}).get("S")
    return TransitionOutcome(transition=Transition.TO_OK, breach_started_at=previous)


def emit_status_change(  # noqa: PLR0913 - the event names the rule, the move, the reading and its file
    client: EventBridgeClient,
    bus_name: str,
    *,
    event_source: str,
    rule: AlertRule,
    outcome: TransitionOutcome,
    value: object,
    source_file: str,
) -> None:
    """Put one status-change event on the alerting bus.

    The detail carries what a downstream rule needs to route on and what a
    person needs to act, so no consumer has to look anything up to triage.

    Args:
        client: EventBridge client.
        bus_name: Alerting event bus name.
        event_source: The ``Source`` field to stamp on the event. The rule that
            routes these events matches on it, so the two are configured
            together and neither is a literal in this code.
        rule: The rule that changed state.
        outcome: The transition that was written.
        value: The reading that moved it.
        source_file: Key of the object the reading came from.
    """
    entity_type, _, entity_id = rule.entity_pk.partition("#")
    to_breach = outcome.transition is Transition.TO_BREACH
    detail = {
        "entity_type": entity_type,
        "entity_id": entity_id,
        "metric": rule.metric,
        "value": str(value),
        "threshold": str(rule.threshold),
        "comparison": rule.comparison,
        "severity": rule.severity,
        "state": AlertState.BREACH.value if to_breach else AlertState.OK.value,
        "breach_started_at": outcome.breach_started_at,
        "source_file": source_file,
    }
    client.put_events(
        Entries=[
            {
                "EventBusName": bus_name,
                "Source": event_source,
                "DetailType": BREACH_DETAIL_TYPE if to_breach else RECOVERY_DETAIL_TYPE,
                "Detail": json.dumps(detail),
            }
        ]
    )
