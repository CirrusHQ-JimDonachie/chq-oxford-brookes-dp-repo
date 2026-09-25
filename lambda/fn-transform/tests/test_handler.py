"""The handler's own job: unpack the notification and report what it did."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import TYPE_CHECKING, Any

import handler
import pytest

from conftest import (
    ERROR_PREFIX,
    RAW_BUCKET,
    RecordingWriter,
    Stack,
    list_keys,
    put_mapping_config,
    put_object,
)

if TYPE_CHECKING:
    from aws_lambda_powertools.utilities.typing import LambdaContext

EVENTS_DIR = Path(__file__).parent / "events"
KEY = "raw/bms/environmental/2025/06/25/nhhb-env.csv"
BODY = b"SensorRef,Timestamp,RoomTemp,RH\nNHHB-2.14,1750845900000,262,55\n"


def load_event(name: str) -> dict[str, Any]:
    """Read one event fixture."""
    event: dict[str, Any] = json.loads((EVENTS_DIR / f"{name}.json").read_text())
    return event


@pytest.fixture
def wired(
    monkeypatch: pytest.MonkeyPatch,
    stack: Stack,
    writer: RecordingWriter,
) -> RecordingWriter:
    """Point the handler's module-level clients at the mocked account."""
    monkeypatch.setattr(handler, "s3_client", stack.s3)
    monkeypatch.setattr(handler, "dynamodb_client", stack.dynamodb)
    monkeypatch.setattr(handler, "events_client", stack.events)
    monkeypatch.setattr(handler, "table_writer", writer)
    return writer


def test_handler_processes_the_object_the_notification_names(
    stack: Stack,
    wired: RecordingWriter,
    lambda_context: LambdaContext,
) -> None:
    """The handler's job is unpacking the record and handing it on."""
    put_mapping_config(stack.dynamodb)
    put_object(stack.s3, KEY, BODY)
    summary = handler.lambda_handler(load_event("s3_object_created"), lambda_context)
    assert summary["objects_processed"] == 1
    assert summary["rows_written"] == 1
    assert wired.rows[0]["source_file"] == KEY


def test_handler_decodes_an_encoded_object_key(
    stack: Stack,
    wired: RecordingWriter,
    lambda_context: LambdaContext,
) -> None:
    """S3 encodes spaces in notification keys; the bucket does not hold them."""
    spaced_key = "raw/bms/environmental/2025/06/25/nhhb env.csv"
    put_mapping_config(stack.dynamodb)
    put_object(stack.s3, spaced_key, BODY)
    event = deepcopy(load_event("s3_object_created"))
    event["Records"][0]["s3"]["object"]["key"] = spaced_key.replace(" ", "+")
    summary = handler.lambda_handler(event, lambda_context)
    assert summary["rows_written"] == 1
    assert wired.rows[0]["source_file"] == spaced_key


def test_handler_reports_a_repeat_delivery_as_already_claimed(
    stack: Stack,
    wired: RecordingWriter,
    lambda_context: LambdaContext,
) -> None:
    """The second delivery is counted separately from a processed object."""
    put_mapping_config(stack.dynamodb)
    put_object(stack.s3, KEY, BODY)
    event = load_event("s3_object_created")
    handler.lambda_handler(event, lambda_context)
    summary = handler.lambda_handler(deepcopy(event), lambda_context)
    assert summary["objects_already_claimed"] == 1
    assert summary["objects_processed"] == 0
    assert len(wired.rows) == 1


@pytest.mark.usefixtures("wired")
def test_handler_counts_a_rejected_file_without_raising(
    stack: Stack,
    lambda_context: LambdaContext,
) -> None:
    """Unsupported data must not reach the dead-letter queue."""
    put_object(stack.s3, KEY, BODY)
    summary = handler.lambda_handler(load_event("s3_object_created"), lambda_context)
    assert summary["records_rejected"] == 1
    assert summary["rows_written"] == 0
    assert f"{ERROR_PREFIX}bms/environmental/2025/06/25/nhhb-env.csv" in list_keys(
        stack.s3, ERROR_PREFIX
    )


@pytest.mark.usefixtures("wired")
def test_handler_raises_when_the_object_is_not_in_the_bucket(
    stack: Stack,
    lambda_context: LambdaContext,
) -> None:
    """A platform failure leaves the invocation so the retry and queue can act."""
    put_mapping_config(stack.dynamodb)
    with pytest.raises(Exception, match=RAW_BUCKET):
        handler.lambda_handler(load_event("s3_object_created"), lambda_context)


@pytest.mark.usefixtures("wired")
def test_handler_names_the_object_on_the_failure_path(
    stack: Stack,
    lambda_context: LambdaContext,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The record that pages someone has to say which drop it was about."""
    put_mapping_config(stack.dynamodb)
    with caplog.at_level("ERROR"), pytest.raises(Exception, match=RAW_BUCKET):
        handler.lambda_handler(load_event("s3_object_created"), lambda_context)
    assert any(
        KEY in record.getMessage() or KEY in str(record.__dict__)
        for record in caplog.records
    )
