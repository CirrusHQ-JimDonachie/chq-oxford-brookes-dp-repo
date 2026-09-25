"""Rule loading, threshold evaluation and the alert-state machine."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from alerting import (
    ALERT_SORT_KEY_PREFIX,
    BREACH_DETAIL_TYPE,
    RECOVERY_DETAIL_TYPE,
    AlertRule,
    AlertState,
    Transition,
    TransitionOutcome,
    apply_transition,
    breaches,
    emit_status_change,
    load_alert_states,
    load_rules,
)

from conftest import (
    CONFIG_TABLE,
    EVENT_BUS,
    EVENT_SOURCE,
    STATE_TABLE,
    Stack,
    put_alert_rule,
)

NOW = datetime(2026, 6, 25, 10, 5, 41, tzinfo=UTC)
ENTITY = "room#NHHB-2.14"
SOURCE_FILE = "raw/bms/environmental/2026/06/25/nhhb-env.csv"

RULE = AlertRule(
    entity_pk=ENTITY,
    metric="temperature_c",
    comparison=">",
    threshold=Decimal("26.0"),
    severity="warning",
    notify_on_recovery=False,
)


def test_load_rules_reads_the_rules_configured_for_the_entity(stack: Stack) -> None:
    """The sort key is the metric, so one entity can carry several rules."""
    put_alert_rule(stack.dynamodb)
    put_alert_rule(
        stack.dynamodb, metric="humidity_pct", comparison="<", threshold=Decimal(30)
    )
    rules = load_rules(stack.dynamodb, CONFIG_TABLE, ENTITY)
    assert {rule.metric for rule in rules} == {"temperature_c", "humidity_pct"}


def test_load_rules_skips_a_rule_with_an_unrecognised_comparison(stack: Stack) -> None:
    """One unusable rule row must not take the whole file down with it."""
    put_alert_rule(stack.dynamodb)
    put_alert_rule(stack.dynamodb, metric="humidity_pct", comparison="approximately")
    rules = load_rules(stack.dynamodb, CONFIG_TABLE, ENTITY)
    assert [rule.metric for rule in rules] == ["temperature_c"]


def test_load_rules_returns_nothing_for_an_entity_with_no_rules(stack: Stack) -> None:
    """Most entities have no thresholds and cost one query and nothing else."""
    assert load_rules(stack.dynamodb, CONFIG_TABLE, ENTITY) == []


def test_breaches_is_true_when_the_reading_crosses_the_threshold() -> None:
    """The worked example's 26.2 against a 26.0 threshold is a breach."""
    assert breaches(Decimal("26.2"), RULE) is True


def test_breaches_is_false_when_the_reading_is_inside_the_threshold() -> None:
    """A reading back under the threshold is the recovery side of the rule."""
    assert breaches(Decimal("24.8"), RULE) is False


def test_breaches_returns_none_when_the_metric_has_no_reading() -> None:
    """A rule on a field this record does not carry is not evaluated."""
    assert breaches(None, RULE) is None


def test_breaches_returns_none_for_a_value_a_threshold_cannot_be_applied_to() -> None:
    """Comparing text to a number would raise; the rule is skipped instead."""
    assert breaches("warm", RULE) is None


def test_apply_transition_moves_an_entity_from_ok_to_breach(stack: Stack) -> None:
    """The first breach writes the state and stamps when the breach began."""
    outcome = apply_transition(
        stack.dynamodb, STATE_TABLE, rule=RULE, is_breaching=True, now=NOW
    )
    assert outcome is not None
    assert outcome.transition is Transition.TO_BREACH
    item = _alert_item(stack)
    assert item["state"]["S"] == AlertState.BREACH.value
    assert item["breach_started_at"]["S"] == NOW.isoformat()


def test_apply_transition_returns_none_when_the_entity_is_already_breaching(
    stack: Stack,
) -> None:
    """A room that stays hot produces no second transition and no second email."""
    apply_transition(stack.dynamodb, STATE_TABLE, rule=RULE, is_breaching=True, now=NOW)
    repeat = apply_transition(
        stack.dynamodb, STATE_TABLE, rule=RULE, is_breaching=True, now=NOW
    )
    assert repeat is None


def test_apply_transition_moves_an_entity_back_to_ok_and_clears_the_breach_stamp(
    stack: Stack,
) -> None:
    """Recovery resets the state so the next breach can alert again."""
    apply_transition(stack.dynamodb, STATE_TABLE, rule=RULE, is_breaching=True, now=NOW)
    outcome = apply_transition(
        stack.dynamodb, STATE_TABLE, rule=RULE, is_breaching=False, now=NOW
    )
    assert outcome is not None
    assert outcome.transition is Transition.TO_OK
    assert outcome.breach_started_at == NOW.isoformat()
    assert "breach_started_at" not in _alert_item(stack)


def test_apply_transition_returns_none_when_recovering_from_no_breach(
    stack: Stack,
) -> None:
    """An entity that was never breaching has nothing to recover from."""
    outcome = apply_transition(
        stack.dynamodb, STATE_TABLE, rule=RULE, is_breaching=False, now=NOW
    )
    assert outcome is None


def test_apply_transition_lets_only_one_of_two_concurrent_readings_win(
    stack: Stack,
) -> None:
    """The loser of the race writes nothing and emits nothing, without erroring."""
    first = apply_transition(
        stack.dynamodb, STATE_TABLE, rule=RULE, is_breaching=True, now=NOW
    )
    second = apply_transition(
        stack.dynamodb, STATE_TABLE, rule=RULE, is_breaching=True, now=NOW
    )
    assert first is not None
    assert second is None


def test_load_alert_states_reads_the_state_of_each_metric(stack: Stack) -> None:
    """The invocation reads state once per entity rather than per reading."""
    apply_transition(stack.dynamodb, STATE_TABLE, rule=RULE, is_breaching=True, now=NOW)
    states = load_alert_states(stack.dynamodb, STATE_TABLE, ENTITY)
    assert states == {"temperature_c": AlertState.BREACH}


def test_load_alert_states_ignores_the_entitys_other_items(stack: Stack) -> None:
    """The snapshot shares the partition and is not an alert state."""
    stack.dynamodb.put_item(
        TableName=STATE_TABLE,
        Item={"pk": {"S": ENTITY}, "sk": {"S": "snapshot"}, "state": {"S": "BREACH"}},
    )
    assert load_alert_states(stack.dynamodb, STATE_TABLE, ENTITY) == {}


def test_emit_status_change_puts_a_breach_event_a_consumer_can_act_on(
    stack: Stack,
) -> None:
    """The detail carries what routes the event and what a person needs to act."""
    captured: list[dict[str, object]] = []
    stack.events.meta.events.register(
        "provide-client-params.events.PutEvents",
        lambda params, **_: captured.append(dict(params)),
    )
    emit_status_change(
        stack.events,
        EVENT_BUS,
        event_source=EVENT_SOURCE,
        rule=RULE,
        outcome=TransitionOutcome(
            transition=Transition.TO_BREACH, breach_started_at=NOW.isoformat()
        ),
        value=Decimal("26.2"),
        source_file=SOURCE_FILE,
    )
    entry = captured[0]["Entries"][0]  # type: ignore[index]  # boto3 params are untyped dicts
    assert entry["DetailType"] == BREACH_DETAIL_TYPE
    assert entry["EventBusName"] == EVENT_BUS
    detail = json.loads(entry["Detail"])
    assert detail["entity_id"] == "NHHB-2.14"
    assert detail["metric"] == "temperature_c"
    assert detail["value"] == "26.2"
    assert detail["threshold"] == "26.0"
    assert detail["comparison"] == ">"
    assert detail["severity"] == "warning"
    assert detail["breach_started_at"] == NOW.isoformat()
    assert detail["source_file"] == SOURCE_FILE


def test_emit_status_change_marks_a_recovery_with_its_own_detail_type(
    stack: Stack,
) -> None:
    """A rule can route recoveries separately from breaches."""
    captured: list[dict[str, object]] = []
    stack.events.meta.events.register(
        "provide-client-params.events.PutEvents",
        lambda params, **_: captured.append(dict(params)),
    )
    emit_status_change(
        stack.events,
        EVENT_BUS,
        event_source=EVENT_SOURCE,
        rule=RULE,
        outcome=TransitionOutcome(
            transition=Transition.TO_OK, breach_started_at=NOW.isoformat()
        ),
        value=Decimal("24.8"),
        source_file=SOURCE_FILE,
    )
    entry = captured[0]["Entries"][0]  # type: ignore[index]  # boto3 params are untyped dicts
    assert entry["DetailType"] == RECOVERY_DETAIL_TYPE


def _alert_item(stack: Stack) -> dict[str, Any]:
    response = stack.dynamodb.get_item(
        TableName=STATE_TABLE,
        Key={
            "pk": {"S": ENTITY},
            "sk": {"S": f"{ALERT_SORT_KEY_PREFIX}temperature_c"},
        },
    )
    return dict(response["Item"])
