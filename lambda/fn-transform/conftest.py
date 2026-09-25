"""Fixtures for the transform engine's tests.

The path shim runs first: the deployment package's contents sit at the function
root once packaged, so the modules import each other by bare name and the tests
have to resolve them the same way. The environment variables are set at import
time because the handler module reads them at module load, which happens the
moment a test imports it.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

import boto3
import pytest
from boto3.dynamodb.types import TypeSerializer
from moto import mock_aws

APP_DIR = Path(__file__).parent / "app"

if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence

    from mypy_boto3_dynamodb.client import DynamoDBClient
    from mypy_boto3_events.client import EventBridgeClient
    from mypy_boto3_s3.client import S3Client

REGION = "eu-west-2"
RAW_BUCKET = "obu-poc-raw"
CONFIG_TABLE = "obu-poc-config"
STATE_TABLE = "obu-poc-state"
EVENT_BUS = "obu-poc-alerts"
EVENT_SOURCE = "obu-poc.building-data"
ERROR_PREFIX = "error/"
METRICS_NAMESPACE = "ObuBuildingData"

ENVIRONMENT = {
    "CONFIG_TABLE_NAME": CONFIG_TABLE,
    "STATE_TABLE_NAME": STATE_TABLE,
    "ALERT_EVENT_BUS_NAME": EVENT_BUS,
    "ALERT_EVENT_SOURCE": EVENT_SOURCE,
    "ICEBERG_CATALOG_URI": f"https://glue.{REGION}.amazonaws.com/iceberg",
    "ICEBERG_WAREHOUSE": "123456789012:s3tablescatalog/obu-curated",
    "ERROR_PREFIX": ERROR_PREFIX,
    "METRICS_NAMESPACE": METRICS_NAMESPACE,
    "CLAIM_TTL_DAYS": "14",
    "AWS_REGION": REGION,
    "AWS_DEFAULT_REGION": REGION,
    "POWERTOOLS_METRICS_NAMESPACE": METRICS_NAMESPACE,
    "POWERTOOLS_SERVICE_NAME": "fn-transform",
}

os.environ.update(ENVIRONMENT)


@dataclass(frozen=True)
class Stack:
    """The mocked account the integration-style tests run against."""

    s3: S3Client
    dynamodb: DynamoDBClient
    events: EventBridgeClient


@pytest.fixture
def stack() -> Iterator[Stack]:
    """Stand up the bucket, both tables and the event bus, all empty."""
    with mock_aws():
        s3 = boto3.client("s3", region_name=REGION)
        s3.create_bucket(
            Bucket=RAW_BUCKET,
            CreateBucketConfiguration={"LocationConstraint": "eu-west-2"},
        )
        dynamodb = boto3.client("dynamodb", region_name=REGION)
        for table_name in (CONFIG_TABLE, STATE_TABLE):
            dynamodb.create_table(
                TableName=table_name,
                KeySchema=[
                    {"AttributeName": "pk", "KeyType": "HASH"},
                    {"AttributeName": "sk", "KeyType": "RANGE"},
                ],
                AttributeDefinitions=[
                    {"AttributeName": "pk", "AttributeType": "S"},
                    {"AttributeName": "sk", "AttributeType": "S"},
                ],
                BillingMode="PAY_PER_REQUEST",
            )
        events = boto3.client("events", region_name=REGION)
        events.create_event_bus(Name=EVENT_BUS)
        yield Stack(s3=s3, dynamodb=dynamodb, events=events)


class RecordingWriter:
    """Stands in for the Iceberg catalog and remembers what it was handed."""

    def __init__(self) -> None:
        """Start with nothing written."""
        self.writes: list[tuple[str, list[Mapping[str, object]]]] = []

    def write(
        self,
        target_table: str,
        fields: Sequence[object],
        rows: Sequence[Mapping[str, object]],
    ) -> None:
        """Record the append instead of making one."""
        del fields
        self.writes.append((target_table, list(rows)))

    @property
    def rows(self) -> list[Mapping[str, object]]:
        """Every row handed over, across all writes."""
        return [row for _, written in self.writes for row in written]


@pytest.fixture
def writer() -> RecordingWriter:
    """A curated-table writer that records rather than writes."""
    return RecordingWriter()


def mapping_config_item() -> dict[str, Any]:
    """The worked example's mapping config, as a plain dictionary."""
    return {
        "input_format": "csv",
        "has_header": True,
        "fields": [
            {"from": "SensorRef", "to": "room_id", "type": "string"},
            {
                "from": "Timestamp",
                "to": "reading_at",
                "type": "timestamp",
                "format": "epoch_ms",
            },
            {
                "from": "RoomTemp",
                "to": "temperature_c",
                "type": "decimal",
                "scale": "0.1",
            },
            {"from": "RH", "to": "humidity_pct", "type": "decimal"},
        ],
        "validation": {
            "required": ["room_id", "reading_at"],
            "ranges": {"temperature_c": {"min": "-20", "max": "60"}},
        },
        "entity": {"type": "room", "key_field": "room_id"},
        "target_table": "building_data.bms_environmental",
    }


def put_mapping_config(
    client: DynamoDBClient,
    *,
    source: str = "bms",
    type_name: str = "environmental",
    item: Mapping[str, Any] | None = None,
) -> None:
    """Write a mapping config into the config table."""
    serializer = TypeSerializer()
    body = dict(item or mapping_config_item())
    body["pk"] = f"config#{source}#{type_name}"
    body["sk"] = "mapping"
    client.put_item(
        TableName=CONFIG_TABLE,
        Item={key: serializer.serialize(value) for key, value in body.items()},
    )


def put_alert_rule(  # noqa: PLR0913 - one parameter per column of a rule row
    client: DynamoDBClient,
    *,
    entity_pk: str = "room#NHHB-2.14",
    metric: str = "temperature_c",
    comparison: str = ">",
    threshold: Decimal = Decimal("26.0"),
    severity: str = "warning",
    notify_on_recovery: bool = False,
) -> None:
    """Write one threshold rule into the config table."""
    client.put_item(
        TableName=CONFIG_TABLE,
        Item={
            "pk": {"S": f"rule#{entity_pk}"},
            "sk": {"S": metric},
            "comparison": {"S": comparison},
            "threshold": {"N": str(threshold)},
            "severity": {"S": severity},
            "notify_on_recovery": {"BOOL": notify_on_recovery},
        },
    )


def put_object(client: S3Client, key: str, body: bytes) -> str | None:
    """Drop an object into the landing bucket and return its version."""
    response = client.put_object(Bucket=RAW_BUCKET, Key=key, Body=body)
    return response.get("VersionId")


def list_keys(client: S3Client, prefix: str) -> list[str]:
    """Every key under a prefix, sorted."""
    response = client.list_objects_v2(Bucket=RAW_BUCKET, Prefix=prefix)
    return sorted(entry["Key"] for entry in response.get("Contents", []))
