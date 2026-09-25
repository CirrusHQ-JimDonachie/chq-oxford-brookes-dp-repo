"""Config-driven transform and threshold engine for the building-data platform.

Triggered by an S3 object-created notification on the raw prefix of the landing
bucket. One invocation claims the object, maps and validates its records against
the mapping config for its source, appends the survivors to the curated Iceberg
table, upserts the current-state snapshot, and emits a status-change event when
a reading moves an entity across one of its thresholds.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, TypedDict
from urllib.parse import unquote_plus

import boto3
from aws_lambda_powertools import Logger, Metrics
from aws_lambda_powertools.metrics import MetricUnit, single_metric
from aws_lambda_powertools.utilities.data_classes import S3Event, event_source
from botocore.config import Config
from config import ObjectRef, Settings
from iceberg_writer import IcebergTableWriter
from transform import (
    Clients,
    Deadline,
    ObjectOutcome,
    Runtime,
    process_object,
    unit_of,
)

if TYPE_CHECKING:
    from aws_lambda_powertools.utilities.typing import LambdaContext
    from mypy_boto3_dynamodb.client import DynamoDBClient
    from mypy_boto3_events.client import EventBridgeClient
    from mypy_boto3_s3.client import S3Client

FUNCTION_ID = "fn-transform"

CORRELATION_ID_PATH = 'Records[0].responseElements."x-amz-request-id"'
"""S3 notifications have no Powertools correlation-path constant, so the request
id S3 stamps on the delivery is used to tie the invocation to the drop."""

DEADLINE_SAFETY_MS = 15_000
"""Time kept back so a failure is logged and raised rather than hard-killed."""

MAX_LOGGED_KEY_LENGTH = 256
"""Object keys are customer-chosen text, so they are capped before logging."""

# read_timeout x total_max_attempts stays well inside the 300s function timeout,
# so the SDK gives up in time for the handler to fail rather than be killed.
BOTO3_CONFIG = Config(
    connect_timeout=3,
    read_timeout=10,
    retries={"total_max_attempts": 4, "mode": "standard"},
)

SETTINGS = Settings.from_env()

s3_client: S3Client = boto3.client("s3", config=BOTO3_CONFIG)
dynamodb_client: DynamoDBClient = boto3.client("dynamodb", config=BOTO3_CONFIG)
events_client: EventBridgeClient = boto3.client("events", config=BOTO3_CONFIG)

table_writer = IcebergTableWriter(
    catalog_uri=SETTINGS.catalog_uri,
    warehouse=SETTINGS.warehouse,
    region=SETTINGS.region,
)

logger = Logger()
metrics = Metrics(namespace=SETTINGS.metrics_namespace)


class InvocationSummary(TypedDict):
    """What the invocation did, for the logs and for a manual replay."""

    objects_processed: int
    objects_already_claimed: int
    rows_written: int
    records_rejected: int
    events_emitted: int


@event_source(data_class=S3Event)
@logger.inject_lambda_context(correlation_id_path=CORRELATION_ID_PATH)
@metrics.log_metrics(capture_cold_start_metric=True)
def lambda_handler(event: S3Event, context: LambdaContext) -> InvocationSummary:
    """Process every object the notification delivered.

    Args:
        event: S3 object-created notification.
        context: Lambda runtime context, read for the remaining-time guard.

    Returns:
        Counts for the invocation. S3 invokes asynchronously and discards the
        return value; it is here for the log record and for local replay.

    Raises:
        Exception: Anything that leaves the invocation, so Lambda retries and
            the failure reaches the dead-letter queue.
    """
    runtime = Runtime(
        settings=SETTINGS,
        clients=Clients(s3=s3_client, dynamodb=dynamodb_client, events=events_client),
        writer=table_writer,
        deadline=Deadline(
            context.get_remaining_time_in_millis, safety_ms=DEADLINE_SAFETY_MS
        ),
        now=datetime.now(tz=UTC),
    )
    summary = InvocationSummary(
        objects_processed=0,
        objects_already_claimed=0,
        rows_written=0,
        records_rejected=0,
        events_emitted=0,
    )

    for record in event.records:
        ref = ObjectRef(
            bucket=record.s3.bucket.name,
            key=unquote_plus(record.s3.get_object.key),
            version_id=record.s3.get_object.version_id,
        )
        unit = {**unit_of(ref), "object_key": _safe(ref.key)}
        metrics.add_metric(name="FilesReceived", unit=MetricUnit.Count, value=1)
        try:
            outcome = process_object(ref, runtime)
        except Exception as exc:
            # Broad on purpose: every failure path must name the object before
            # it leaves, and the unit identifiers ride on this engine's own
            # exceptions rather than being re-derived here.
            logger.exception(
                "object failed",
                extra={
                    **getattr(exc, "unit", {}),
                    **unit,
                    "function_id": FUNCTION_ID,
                    "operation": "object.transform",
                    "outcome": "failure",
                },
            )
            raise
        _record_outcome(summary, outcome, unit=unit)

    return summary


def _record_outcome(
    summary: InvocationSummary,
    outcome: ObjectOutcome,
    *,
    unit: dict[str, object],
) -> None:
    """Fold one object's result into the summary, logs and metrics."""
    if not outcome.claimed:
        summary["objects_already_claimed"] += 1
        logger.info(
            "object already processed",
            extra={**unit, "function_id": FUNCTION_ID, "operation": "object.claim"},
        )
        return

    summary["objects_processed"] += 1
    summary["rows_written"] += outcome.rows_written
    summary["records_rejected"] += outcome.records_rejected
    summary["events_emitted"] += outcome.events_emitted

    _publish_rejections(outcome)

    logger.info(
        "object processed",
        extra={
            **unit,
            "function_id": FUNCTION_ID,
            "operation": "object.transform",
            "outcome": "failure" if outcome.file_reason else "success",
            "source": outcome.source,
            "rows_written": outcome.rows_written,
            "records_rejected": outcome.records_rejected,
            "events_emitted": outcome.events_emitted,
            "file_reason": str(outcome.file_reason) if outcome.file_reason else None,
        },
    )


def _publish_rejections(outcome: ObjectOutcome) -> None:
    """Count rejected records per source and reason, as embedded metric format.

    Each dimension pair is its own series for triage; the undimensioned total
    is the series the alarm watches.
    """
    if outcome.records_rejected:
        # The undimensioned total is what the data-quality alarm watches; an
        # alarm cannot aggregate across dimension pairs it does not know yet.
        metrics.add_metric(
            name="RejectedRecords",
            unit=MetricUnit.Count,
            value=outcome.records_rejected,
        )
    for reason, count in outcome.rejections_by_reason.items():
        with single_metric(
            name="RejectedRecords",
            unit=MetricUnit.Count,
            value=count,
            namespace=SETTINGS.metrics_namespace,
        ) as metric:
            metric.add_dimension(name="source", value=outcome.source)
            metric.add_dimension(name="reason", value=reason)


def _safe(text: str) -> str:
    """Cap and flatten a customer-supplied value before it reaches a log field."""
    flattened = "".join(character for character in text if character.isprintable())
    if len(flattened) <= MAX_LOGGED_KEY_LENGTH:
        return flattened
    return f"{flattened[:MAX_LOGGED_KEY_LENGTH]}..."
