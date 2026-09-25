"""One dropped object, end to end: claim, map, write, snapshot and alert."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from unittest import mock

import pytest
from botocore.exceptions import BotoCoreError
from config import ObjectRef, Settings
from exceptions import (
    CatalogWriteError,
    DeadlineExceededError,
    ObjectInFlightError,
    ObjectReadError,
)
from reasons import ReasonCode
from state import ClaimStatus, claim_key, claim_object
from transform import Clients, Deadline, ObjectOutcome, Runtime, process_object

from conftest import (
    ERROR_PREFIX,
    RAW_BUCKET,
    STATE_TABLE,
    RecordingWriter,
    Stack,
    list_keys,
    mapping_config_item,
    put_alert_rule,
    put_mapping_config,
    put_object,
)

KEY = "raw/bms/environmental/2025/06/25/nhhb-env.csv"
RELATIVE_KEY = "bms/environmental/2025/06/25/nhhb-env.csv"
NOW = datetime(2025, 6, 25, 10, 5, 41, tzinfo=UTC)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

HEADER = "SensorRef,Timestamp,RoomTemp,RH\n"
WARM_ROW = "NHHB-2.14,1750845900000,262,55\n"
COOL_ROW = "NHHB-2.14,1750845900000,248,55\n"


@pytest.fixture
def settings() -> Settings:
    """Settings built from the test environment."""
    return Settings.from_env()


@pytest.fixture
def runtime(stack: Stack, settings: Settings, writer: RecordingWriter) -> Runtime:
    """The invocation-wide wiring, with plenty of time left on the clock."""
    return Runtime(
        settings=settings,
        clients=Clients(s3=stack.s3, dynamodb=stack.dynamodb, events=stack.events),
        writer=writer,
        deadline=Deadline(lambda: 300_000, safety_ms=15_000),
        now=NOW,
    )


@pytest.fixture
def captured_events(stack: Stack) -> list[dict[str, object]]:
    """Every PutEvents call the transform makes."""
    captured: list[dict[str, object]] = []
    stack.events.meta.events.register(
        "provide-client-params.events.PutEvents",
        lambda params, **_: captured.append(dict(params)),
    )
    return captured


def run(
    runtime: Runtime,
    *,
    key: str = KEY,
    version_id: str | None = None,
) -> ObjectOutcome:
    """Process one object with the test's wiring."""
    return process_object(
        ObjectRef(bucket=RAW_BUCKET, key=key, version_id=version_id), runtime
    )


def test_process_object_writes_the_mapped_row_to_the_curated_table(
    stack: Stack,
    writer: RecordingWriter,
    runtime: Runtime,
) -> None:
    """The worked example's row reaches the curated table with its provenance."""
    put_mapping_config(stack.dynamodb)
    put_object(stack.s3, KEY, (HEADER + WARM_ROW).encode())
    outcome = run(runtime)
    assert outcome.rows_written == 1
    row = writer.rows[0]
    assert row["room_id"] == "NHHB-2.14"
    assert row["temperature_c"] == Decimal("26.2")
    assert row["source_file"] == KEY
    assert row["ingested_at"] == NOW


def test_process_object_upserts_the_snapshot_for_the_entity(
    stack: Stack,
    runtime: Runtime,
) -> None:
    """The hot store carries the latest reading the read API serves."""
    put_mapping_config(stack.dynamodb)
    put_object(stack.s3, KEY, (HEADER + WARM_ROW).encode())
    run(runtime)
    item = stack.dynamodb.get_item(
        TableName=STATE_TABLE,
        Key={"pk": {"S": "room#NHHB-2.14"}, "sk": {"S": "snapshot"}},
    )["Item"]
    assert item["temperature_c"]["N"] == "26.2"


def test_process_object_does_nothing_for_an_object_already_processed(
    stack: Stack,
    writer: RecordingWriter,
    runtime: Runtime,
) -> None:
    """A repeat delivery writes nothing twice, curated rows included."""
    put_mapping_config(stack.dynamodb)
    put_object(stack.s3, KEY, (HEADER + WARM_ROW).encode())
    run(runtime)
    repeat = run(runtime)
    assert repeat.claimed is False
    assert len(writer.rows) == 1


def test_process_object_marks_the_claim_done_on_success(
    stack: Stack,
    runtime: Runtime,
) -> None:
    """Only a completed object is allowed to suppress a later delivery."""
    put_mapping_config(stack.dynamodb)
    put_object(stack.s3, KEY, (HEADER + WARM_ROW).encode())
    run(runtime)
    assert _claim_status(stack) == ClaimStatus.DONE.value


def test_process_object_marks_the_claim_done_after_a_whole_file_rejection(
    stack: Stack,
    runtime: Runtime,
) -> None:
    """An unsupported file is handled, so re-delivering it must not redo it."""
    put_object(stack.s3, KEY, (HEADER + WARM_ROW).encode())
    run(runtime)
    assert _claim_status(stack) == ClaimStatus.DONE.value


def test_process_object_releases_the_claim_when_the_curated_write_fails(
    stack: Stack,
    runtime: Runtime,
) -> None:
    """The retry must re-process, not find the object claimed and do nothing."""
    put_mapping_config(stack.dynamodb)
    put_object(stack.s3, KEY, (HEADER + WARM_ROW).encode())
    with pytest.raises(CatalogWriteError):
        run(replace(runtime, writer=RefusingWriter()))
    assert _claim_item(stack) is None


def test_process_object_reprocesses_fully_after_a_failed_attempt(
    stack: Stack,
    writer: RecordingWriter,
    runtime: Runtime,
) -> None:
    """The file reaches the curated table exactly once, not zero times."""
    put_mapping_config(stack.dynamodb)
    put_object(stack.s3, KEY, (HEADER + WARM_ROW).encode())
    with pytest.raises(CatalogWriteError):
        run(replace(runtime, writer=RefusingWriter()))
    outcome = run(runtime)
    assert outcome.claimed is True
    assert outcome.rows_written == 1
    assert len(writer.rows) == 1


def test_process_object_keeps_the_original_failure_when_the_release_also_fails(
    stack: Stack,
    runtime: Runtime,
) -> None:
    """A failed release must not hide what actually went wrong."""
    put_mapping_config(stack.dynamodb)
    put_object(stack.s3, KEY, (HEADER + WARM_ROW).encode())
    failing = replace(runtime, writer=RefusingWriter())
    with (
        mock.patch.object(
            failing.clients.dynamodb, "delete_item", side_effect=BotoCoreError()
        ),
        pytest.raises(CatalogWriteError),
    ):
        run(failing)


def test_process_object_gives_way_while_another_invocation_holds_the_object(
    stack: Stack,
    runtime: Runtime,
) -> None:
    """Two deliveries of the same object version do not both process it."""
    put_mapping_config(stack.dynamodb)
    put_object(stack.s3, KEY, (HEADER + WARM_ROW).encode())
    claim_object(
        stack.dynamodb,
        STATE_TABLE,
        ref=ObjectRef(bucket=RAW_BUCKET, key=KEY, version_id=None),
        ttl_days=14,
        now=NOW,
    )
    with pytest.raises(ObjectInFlightError):
        run(runtime)


def test_process_object_emits_one_event_on_the_first_breach(
    stack: Stack,
    runtime: Runtime,
    captured_events: list[dict[str, object]],
) -> None:
    """The OK to BREACH transition is what raises the alert."""
    put_mapping_config(stack.dynamodb)
    put_alert_rule(stack.dynamodb)
    put_object(stack.s3, KEY, (HEADER + WARM_ROW).encode())
    outcome = run(runtime)
    assert outcome.events_emitted == 1
    detail = json.loads(captured_events[0]["Entries"][0]["Detail"])  # type: ignore[index]
    assert detail["value"] == "26.2"


def test_process_object_stamps_the_configured_source_on_the_event(
    stack: Stack,
    runtime: Runtime,
    captured_events: list[dict[str, object]],
) -> None:
    """The routing rule matches on this, so it is deploy-time configuration."""
    put_mapping_config(stack.dynamodb)
    put_alert_rule(stack.dynamodb)
    put_object(stack.s3, KEY, (HEADER + WARM_ROW).encode())
    run(
        replace(
            runtime, settings=replace(runtime.settings, alert_event_source="tenant.x")
        )
    )
    entry = captured_events[0]["Entries"][0]  # type: ignore[index]
    assert entry["Source"] == "tenant.x"


def test_process_object_stays_silent_while_the_entity_remains_in_breach(
    stack: Stack,
    runtime: Runtime,
) -> None:
    """A room that stays hot produces no second email."""
    put_mapping_config(stack.dynamodb)
    put_alert_rule(stack.dynamodb)
    put_object(stack.s3, KEY, (HEADER + WARM_ROW).encode())
    run(runtime)
    second_key = KEY.replace("nhhb-env", "nhhb-env-2")
    put_object(stack.s3, second_key, (HEADER + WARM_ROW).encode())
    outcome = run(runtime, key=second_key)
    assert outcome.events_emitted == 0


def test_process_object_emits_no_recovery_event_unless_the_rule_asks_for_one(
    stack: Stack,
    runtime: Runtime,
) -> None:
    """Recovery still resets the state so the next breach alerts again."""
    put_mapping_config(stack.dynamodb)
    put_alert_rule(stack.dynamodb)
    put_object(stack.s3, KEY, (HEADER + WARM_ROW).encode())
    run(runtime)
    cool_key = KEY.replace("nhhb-env", "nhhb-env-cool")
    put_object(stack.s3, cool_key, (HEADER + COOL_ROW).encode())
    outcome = run(runtime, key=cool_key)
    assert outcome.events_emitted == 0
    item = stack.dynamodb.get_item(
        TableName=STATE_TABLE,
        Key={"pk": {"S": "room#NHHB-2.14"}, "sk": {"S": "alert#temperature_c"}},
    )["Item"]
    assert item["state"]["S"] == "OK"


def test_process_object_emits_a_recovery_event_when_the_rule_asks_for_one(
    stack: Stack,
    runtime: Runtime,
    captured_events: list[dict[str, object]],
) -> None:
    """Recovery notification is a per-rule choice, off unless configured."""
    put_mapping_config(stack.dynamodb)
    put_alert_rule(stack.dynamodb, notify_on_recovery=True)
    put_object(stack.s3, KEY, (HEADER + WARM_ROW).encode())
    run(runtime)
    cool_key = KEY.replace("nhhb-env", "nhhb-env-cool")
    put_object(stack.s3, cool_key, (HEADER + COOL_ROW).encode())
    outcome = run(runtime, key=cool_key)
    assert outcome.events_emitted == 1
    detail = json.loads(captured_events[-1]["Entries"][0]["Detail"])  # type: ignore[index]
    assert detail["state"] == "OK"


def test_process_object_sends_a_file_with_no_mapping_config_to_the_error_prefix(
    stack: Stack,
    writer: RecordingWriter,
    runtime: Runtime,
) -> None:
    """An unsupported source is a data problem, so the invocation succeeds."""
    put_object(stack.s3, KEY, (HEADER + WARM_ROW).encode())
    outcome = run(runtime)
    assert outcome.file_reason is ReasonCode.NO_MAPPING_CONFIG
    assert writer.rows == []
    assert list_keys(stack.s3, ERROR_PREFIX) == [
        f"{ERROR_PREFIX}{RELATIVE_KEY}",
        f"{ERROR_PREFIX}{RELATIVE_KEY}.rejected.json",
    ]


def test_process_object_records_the_reason_beside_the_rejected_file(
    stack: Stack,
    runtime: Runtime,
) -> None:
    """The reason code is machine-readable so the alarm can dimension on it."""
    put_object(stack.s3, KEY, (HEADER + WARM_ROW).encode())
    run(runtime)
    body = stack.s3.get_object(
        Bucket=RAW_BUCKET, Key=f"{ERROR_PREFIX}{RELATIVE_KEY}.rejected.json"
    )["Body"].read()
    assert json.loads(body)["reason_code"] == ReasonCode.NO_MAPPING_CONFIG.value


def test_process_object_rejects_a_key_outside_the_agreed_shape(
    stack: Stack,
    runtime: Runtime,
) -> None:
    """A key with no source and type cannot be mapped to any config."""
    odd_key = "raw/loose-file.csv"
    put_object(stack.s3, odd_key, (HEADER + WARM_ROW).encode())
    outcome = run(runtime, key=odd_key)
    assert outcome.file_reason is ReasonCode.MALFORMED_KEY


def test_process_object_rejects_a_config_that_does_not_describe_a_mapping(
    stack: Stack,
    runtime: Runtime,
) -> None:
    """A broken config row is reported against the file, not raised."""
    broken = {k: v for k, v in mapping_config_item().items() if k != "entity"}
    put_mapping_config(stack.dynamodb, item=broken)
    put_object(stack.s3, KEY, (HEADER + WARM_ROW).encode())
    outcome = run(runtime)
    assert outcome.file_reason is ReasonCode.INVALID_MAPPING_CONFIG


def test_process_object_rejects_a_file_that_is_not_its_declared_format(
    stack: Stack,
    runtime: Runtime,
) -> None:
    """An unreadable file is counted and set aside, not retried forever."""
    put_mapping_config(
        stack.dynamodb, item={**mapping_config_item(), "input_format": "json"}
    )
    put_object(stack.s3, KEY, b"SensorRef,RoomTemp\nA,1\n")
    outcome = run(runtime)
    assert outcome.file_reason is ReasonCode.UNPARSEABLE_FILE


def test_process_object_keeps_the_good_records_of_a_partly_bad_file(
    stack: Stack,
    runtime: Runtime,
) -> None:
    """One bad row costs that row, not the drop."""
    put_mapping_config(stack.dynamodb)
    body = HEADER + WARM_ROW + "NHHB-2.15,1750845900000,7000,55\n"
    put_object(stack.s3, KEY, body.encode())
    outcome = run(runtime)
    assert outcome.rows_written == 1
    assert outcome.rejections_by_reason == {ReasonCode.VALUE_OUT_OF_RANGE.value: 1}


def test_process_object_writes_rejected_records_with_their_provenance(
    stack: Stack,
    runtime: Runtime,
) -> None:
    """A data owner can find the row that failed and why, from the file alone."""
    put_mapping_config(stack.dynamodb)
    body = HEADER + WARM_ROW + "NHHB-2.15,1750845900000,7000,55\n"
    put_object(stack.s3, KEY, body.encode())
    run(runtime)
    rejects = stack.s3.get_object(
        Bucket=RAW_BUCKET, Key=f"{ERROR_PREFIX}{RELATIVE_KEY}.rejects.jsonl"
    )["Body"].read()
    entry = json.loads(rejects.decode().splitlines()[0])
    assert entry["reason_code"] == ReasonCode.VALUE_OUT_OF_RANGE.value
    assert entry["source_key"] == KEY
    assert entry["record_index"] == 1
    assert entry["record"]["RoomTemp"] == "7000"


def test_process_object_raises_when_the_object_cannot_be_read(
    stack: Stack,
    runtime: Runtime,
) -> None:
    """A read failure is a platform failure and belongs on the queue."""
    put_mapping_config(stack.dynamodb)
    with pytest.raises(ObjectReadError, match=KEY):
        run(runtime)


def test_process_object_raises_rather_than_truncating_against_the_deadline(
    stack: Stack,
    runtime: Runtime,
) -> None:
    """A half-written file would be lost silently, because the claim is held."""
    put_mapping_config(stack.dynamodb)
    put_object(stack.s3, KEY, (HEADER + WARM_ROW).encode())
    spent_runtime = replace(runtime, deadline=Deadline(lambda: 1_000, safety_ms=15_000))
    with pytest.raises(DeadlineExceededError) as caught:
        run(spent_runtime)
    assert caught.value.unit["object_key"] == KEY


def test_process_object_ignores_rules_for_an_entity_the_file_does_not_carry(
    stack: Stack,
    runtime: Runtime,
) -> None:
    """Rules are looked up per entity seen, so unrelated rooms cost nothing."""
    put_mapping_config(stack.dynamodb)
    put_alert_rule(stack.dynamodb, entity_pk="room#OTHER-1.01")
    put_object(stack.s3, KEY, (HEADER + WARM_ROW).encode())
    assert run(runtime).events_emitted == 0


def test_process_object_reads_the_delivered_version_of_the_object(
    stack: Stack,
    writer: RecordingWriter,
    runtime: Runtime,
) -> None:
    """The claim is on a version, so the read has to be on the same one."""
    put_mapping_config(stack.dynamodb)
    stack.s3.put_bucket_versioning(
        Bucket=RAW_BUCKET, VersioningConfiguration={"Status": "Enabled"}
    )
    first_version = put_object(stack.s3, KEY, (HEADER + WARM_ROW).encode())
    put_object(stack.s3, KEY, (HEADER + COOL_ROW).encode())
    run(runtime, version_id=first_version)
    assert writer.rows[0]["temperature_c"] == Decimal("26.2")


def test_process_object_counts_every_rejection_reason_separately(
    stack: Stack,
    runtime: Runtime,
) -> None:
    """The metric is dimensioned by reason, so the counts are kept apart."""
    put_mapping_config(stack.dynamodb)
    body = HEADER + WARM_ROW + ",1750845900000,262,55\n" + "NHHB-2.15,x,262,55\n"
    put_object(stack.s3, KEY, body.encode())
    outcome = run(runtime)
    assert outcome.rejections_by_reason == {
        ReasonCode.MISSING_REQUIRED_FIELD.value: 1,
        ReasonCode.TYPE_COERCION_FAILED.value: 1,
    }
    assert outcome.records_rejected == 2


class RefusingWriter:
    """A curated-table writer standing in for an unreachable REST catalog."""

    def write(
        self,
        target_table: str,
        fields: Sequence[object],
        rows: Sequence[Mapping[str, object]],
    ) -> None:
        """Fail the way a catalog outage does."""
        del fields, rows
        msg = f"Curated write to {target_table} failed"
        raise CatalogWriteError(msg)


def _claim_item(stack: Stack) -> dict[str, Any] | None:
    ref = ObjectRef(bucket=RAW_BUCKET, key=KEY, version_id=None)
    response = stack.dynamodb.get_item(
        TableName=STATE_TABLE,
        Key={"pk": {"S": claim_key(ref)}, "sk": {"S": "claim"}},
        ConsistentRead=True,
    )
    return dict(response["Item"]) if "Item" in response else None


def _claim_status(stack: Stack) -> str | None:
    item = _claim_item(stack)
    return None if item is None else str(item["status"]["S"])
