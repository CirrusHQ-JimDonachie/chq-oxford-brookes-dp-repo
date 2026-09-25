"""What one dropped object does, from the claim through to the alert events.

The cold path (the curated Iceberg append) and the hot path (snapshot upsert,
threshold evaluation, status-change events) both run here, off the one file-drop
invocation.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import alerting
import errors
import state
from aws_lambda_powertools import Logger
from botocore.exceptions import BotoCoreError, ClientError
from config import load_mapping_config, parse_object_key
from exceptions import (
    DeadlineExceededError,
    FileFormatError,
    MappingConfigError,
    ObjectReadError,
)
from iceberg_writer import provenance
from mapping import Rejection, entity_key, map_record, parse_records
from reasons import ReasonCode

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from datetime import datetime

    from config import MappingConfig, ObjectRef, Settings
    from iceberg_writer import TableWriter
    from mypy_boto3_dynamodb.client import DynamoDBClient
    from mypy_boto3_events.client import EventBridgeClient
    from mypy_boto3_s3.client import S3Client

DEADLINE_CHECK_INTERVAL = 1_000
"""How many records to process between remaining-time checks."""

UNKNOWN_SOURCE = "unknown"
"""Metric dimension for a file rejected before its source could be read."""

logger = Logger(child=True)


@dataclass(frozen=True)
class Clients:
    """The AWS clients one object's processing needs."""

    s3: S3Client
    dynamodb: DynamoDBClient
    events: EventBridgeClient


class Deadline:
    """Remaining invocation time, with the threshold work must stop above."""

    def __init__(self, remaining_ms: Callable[[], int], *, safety_ms: int) -> None:
        """Hold the runtime's clock rather than the Lambda context itself.

        Args:
            remaining_ms: Returns milliseconds left in the invocation.
            safety_ms: Margin below which no further work is started.
        """
        self._remaining_ms = remaining_ms
        self._safety_ms = safety_ms

    def check(self, unit: Mapping[str, object]) -> None:
        """Stop the invocation while there is still time to fail cleanly.

        Raises:
            DeadlineExceededError: Less than the safety margin remains.
        """
        remaining = self._remaining_ms()
        if remaining < self._safety_ms:
            msg = f"Stopped with {remaining}ms left rather than truncate the object"
            raise DeadlineExceededError(msg, unit=unit)


@dataclass(frozen=True)
class Runtime:
    """Everything that is fixed for the whole invocation."""

    settings: Settings
    clients: Clients
    writer: TableWriter
    deadline: Deadline
    now: datetime


@dataclass
class ObjectOutcome:
    """What processing one object did, for the invocation summary and logs."""

    claimed: bool = True
    source: str = UNKNOWN_SOURCE
    rows_written: int = 0
    events_emitted: int = 0
    file_reason: ReasonCode | None = None
    rejections_by_reason: Counter[str] = field(default_factory=Counter)

    @property
    def records_rejected(self) -> int:
        """How many records, or whole files, were sent to the error prefix."""
        return sum(self.rejections_by_reason.values())


def process_object(ref: ObjectRef, runtime: Runtime) -> ObjectOutcome:
    """Run one dropped object through both paths, under a claim.

    The claim is taken first and is only marked done once the object is fully
    processed. Any failure releases it, so Lambda's retry and a dead-letter
    redrive both re-process the object from the start rather than finding it
    already claimed and doing nothing.

    Args:
        ref: The delivered object.
        runtime: Settings, clients, the curated-table writer, the remaining-time
            guard and the invocation timestamp.

    Returns:
        What was written, rejected and emitted.

    Raises:
        ObjectInFlightError: Another invocation is mid-flight on this object.
        ObjectReadError: The object could not be read from the bucket.
        DeadlineExceededError: The object is too large to finish safely.
    """
    settings = runtime.settings
    clients = runtime.clients

    claim = state.claim_object(
        clients.dynamodb,
        settings.state_table,
        ref=ref,
        ttl_days=settings.claim_ttl_days,
        now=runtime.now,
    )
    if claim is state.ClaimOutcome.ALREADY_DONE:
        return ObjectOutcome(claimed=False)

    try:
        outcome = _process_claimed(ref, runtime)
    except Exception:
        # The claim goes back so the retry can re-process. Releasing is best
        # effort and never replaces the failure being handled.
        if not state.release_claim(clients.dynamodb, settings.state_table, ref=ref):
            logger.warning(
                "claim not released after failure",
                extra={**unit_of(ref), "operation": "object.claim"},
            )
        raise

    if not state.complete_claim(
        clients.dynamodb, settings.state_table, ref=ref, now=runtime.now
    ):
        logger.warning(
            "claim was taken over before completion",
            extra={**unit_of(ref), "operation": "object.claim"},
        )
    return outcome


def _process_claimed(ref: ObjectRef, runtime: Runtime) -> ObjectOutcome:
    """Everything the object needs once this invocation owns its claim."""
    settings = runtime.settings
    clients = runtime.clients

    path = parse_object_key(ref.key)
    if path is None:
        return _reject_whole_file(
            runtime,
            ref,
            reason=ReasonCode.MALFORMED_KEY,
            detail="Key is not raw/{source}/{type}/yyyy/mm/dd/{filename}",
            source=UNKNOWN_SOURCE,
        )

    try:
        config = load_mapping_config(
            clients.dynamodb, settings.config_table, path.source, path.type
        )
    except MappingConfigError as exc:
        return _reject_whole_file(
            runtime,
            ref,
            reason=ReasonCode.INVALID_MAPPING_CONFIG,
            detail=str(exc),
            source=path.source,
        )
    if config is None:
        return _reject_whole_file(
            runtime,
            ref,
            reason=ReasonCode.NO_MAPPING_CONFIG,
            detail=f"No mapping config for source {path.source} and type {path.type}",
            source=path.source,
        )

    body = _read_object(clients.s3, ref)

    try:
        raw_records = parse_records(body, config)
    except FileFormatError as exc:
        return _reject_whole_file(
            runtime,
            ref,
            reason=ReasonCode.UNPARSEABLE_FILE,
            detail=str(exc),
            source=path.source,
        )

    mapped: list[dict[str, object]] = []
    rejections: list[Rejection] = []
    for position, raw in enumerate(raw_records):
        if position % DEADLINE_CHECK_INTERVAL == 0:
            runtime.deadline.check(unit_of(ref))
        result = map_record(raw, config)
        if isinstance(result, Rejection):
            rejections.append(result)
        else:
            mapped.append(result)

    outcome = ObjectOutcome(source=path.source, rows_written=len(mapped))
    outcome.rejections_by_reason.update(
        str(rejection.reason) for rejection in rejections
    )

    errors.write_record_rejections(
        clients.s3,
        ref=ref,
        path=path,
        error_prefix=settings.error_prefix,
        rejections=rejections,
    )

    if mapped:
        runtime.writer.write(
            config.target_table,
            config.fields,
            [{**row, **provenance(ref.key, runtime.now)} for row in mapped],
        )
        _upsert_snapshots(runtime, config, mapped, ref)
        outcome.events_emitted = _run_alerts(runtime, config, mapped, ref)

    return outcome


def unit_of(ref: ObjectRef) -> dict[str, object]:
    """The identifiers every record about this object is logged against."""
    return {"bucket": ref.bucket, "object_key": ref.key}


def _read_object(client: S3Client, ref: ObjectRef) -> bytes:
    """Read the delivered version of the object, not whatever is current."""
    try:
        if ref.version_id is not None:
            response = client.get_object(
                Bucket=ref.bucket, Key=ref.key, VersionId=ref.version_id
            )
        else:
            response = client.get_object(Bucket=ref.bucket, Key=ref.key)
        return response["Body"].read()
    except (BotoCoreError, ClientError, OSError) as exc:
        msg = f"Could not read s3://{ref.bucket}/{ref.key}"
        raise ObjectReadError(msg, unit=unit_of(ref)) from exc


def _reject_whole_file(
    runtime: Runtime,
    ref: ObjectRef,
    *,
    reason: ReasonCode,
    detail: str,
    source: str,
) -> ObjectOutcome:
    """Send an unusable file to the error prefix and report it as handled.

    This returns rather than raising: an unsupported or unreadable file is a
    data problem, and raising would send it to the dead-letter queue that
    exists for platform failures.
    """
    errors.reject_file(
        runtime.clients.s3,
        ref=ref,
        error_prefix=runtime.settings.error_prefix,
        reason=reason,
        detail=detail,
    )
    outcome = ObjectOutcome(source=source, file_reason=reason)
    outcome.rejections_by_reason[str(reason)] = 1
    return outcome


def _upsert_snapshots(
    runtime: Runtime,
    config: MappingConfig,
    mapped: Sequence[Mapping[str, object]],
    ref: ObjectRef,
) -> None:
    """Write one current-state item per entity, from its last reading in the file."""
    latest: dict[str, Mapping[str, object]] = {}
    for row in mapped:
        entity_pk = entity_key(config, row)
        if entity_pk is not None:
            latest[entity_pk] = row
    for entity_pk, row in latest.items():
        state.upsert_snapshot(
            runtime.clients.dynamodb,
            runtime.settings.state_table,
            entity_pk=entity_pk,
            entity_type=config.entity.type,
            values=row,
            source_file=ref.key,
            now=runtime.now,
        )


def _run_alerts(
    runtime: Runtime,
    config: MappingConfig,
    mapped: Sequence[Mapping[str, object]],
    ref: ObjectRef,
) -> int:
    """Evaluate every reading against its rules and emit the transitions.

    Rules and current state are read once per entity, and the conditional write
    is issued only where the reading moves the state the invocation last saw.
    A file whose readings all sit on the same side of a threshold therefore
    costs one read and no writes, while an oscillating one still produces a
    transition per genuine flip.
    """
    clients = runtime.clients
    settings = runtime.settings
    rules_by_entity: dict[str, list[alerting.AlertRule]] = {}
    known_state: dict[tuple[str, str], alerting.AlertState] = {}
    emitted = 0

    for position, row in enumerate(mapped):
        if position % DEADLINE_CHECK_INTERVAL == 0:
            runtime.deadline.check(unit_of(ref))
        entity_pk = entity_key(config, row)
        if entity_pk is None:
            continue
        if entity_pk not in rules_by_entity:
            rules_by_entity[entity_pk] = alerting.load_rules(
                clients.dynamodb, settings.config_table, entity_pk
            )
            for metric, current in alerting.load_alert_states(
                clients.dynamodb, settings.state_table, entity_pk
            ).items():
                known_state[entity_pk, metric] = current

        for rule in rules_by_entity[entity_pk]:
            emitted += _apply_rule(runtime, rule, row, known_state, ref)
    return emitted


def _apply_rule(
    runtime: Runtime,
    rule: alerting.AlertRule,
    row: Mapping[str, object],
    known_state: dict[tuple[str, str], alerting.AlertState],
    ref: ObjectRef,
) -> int:
    """Move one rule's state if this reading changes it, and emit if it moved."""
    is_breaching = alerting.breaches(row.get(rule.metric), rule)
    if is_breaching is None:
        return 0

    current = known_state.get((rule.entity_pk, rule.metric), alerting.AlertState.OK)
    wanted = alerting.AlertState.BREACH if is_breaching else alerting.AlertState.OK
    if current is wanted:
        return 0

    outcome = alerting.apply_transition(
        runtime.clients.dynamodb,
        runtime.settings.state_table,
        rule=rule,
        is_breaching=is_breaching,
        now=runtime.now,
    )
    known_state[rule.entity_pk, rule.metric] = wanted
    if outcome is None:
        return 0
    if outcome.transition is alerting.Transition.TO_OK and not rule.notify_on_recovery:
        return 0

    alerting.emit_status_change(
        runtime.clients.events,
        runtime.settings.event_bus,
        event_source=runtime.settings.alert_event_source,
        rule=rule,
        outcome=outcome,
        value=row.get(rule.metric),
        source_file=ref.key,
    )
    return 1
