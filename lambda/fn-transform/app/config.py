"""Environment settings, object-key parsing and the mapping-config contract.

The mapping config is the whole extension point of this engine: onboarding a
source means writing one config item, never adding code. Anything the config
cannot express is a rejection.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from boto3.dynamodb.types import TypeDeserializer
from exceptions import ConfigurationError, MappingConfigError

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from mypy_boto3_dynamodb.client import DynamoDBClient

REQUIRED_ENV_VARS = (
    "CONFIG_TABLE_NAME",
    "STATE_TABLE_NAME",
    "ALERT_EVENT_BUS_NAME",
    "ALERT_EVENT_SOURCE",
    "ICEBERG_CATALOG_URI",
    "ICEBERG_WAREHOUSE",
    "ERROR_PREFIX",
    "METRICS_NAMESPACE",
    "CLAIM_TTL_DAYS",
)

# The only key shape the landing bucket serves. The S3 notification is filtered
# to the raw/ root; anything else reaching the engine is rejected on the key.
OBJECT_KEY_PATTERN = re.compile(
    r"^raw/(?P<source>[^/]+)/(?P<type>[^/]+)/"
    r"(?P<year>\d{4})/(?P<month>\d{2})/(?P<day>\d{2})/(?P<filename>[^/]+)$"
)

RAW_ROOT = "raw/"

UNVERSIONED = "null"
"""Stand-in version id for an object in a bucket without versioning."""

MAPPING_CONFIG_SORT_KEY = "mapping"
"""Sort key of a mapping-config item, matching the snapshot item's convention."""


class InputFormat(StrEnum):
    """File formats the engine can read."""

    CSV = "csv"
    JSON = "json"
    JSONL = "jsonl"


class FieldType(StrEnum):
    """Types a mapped field can be coerced to."""

    STRING = "string"
    DECIMAL = "decimal"
    INTEGER = "integer"
    TIMESTAMP = "timestamp"
    BOOLEAN = "boolean"


class TimestampFormat(StrEnum):
    """Timestamp encodings the engine can read."""

    EPOCH_MS = "epoch_ms"
    EPOCH_S = "epoch_s"
    ISO8601 = "iso8601"


@dataclass(frozen=True)
class Settings:
    """Deployment-time configuration, read once at module load."""

    config_table: str
    state_table: str
    event_bus: str
    alert_event_source: str
    catalog_uri: str
    warehouse: str
    error_prefix: str
    metrics_namespace: str
    claim_ttl_days: int
    region: str

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        """Read and validate every required environment variable.

        Args:
            env: Mapping to read from. Defaults to the process environment.

        Returns:
            Validated settings.

        Raises:
            ConfigurationError: A required variable is missing or empty, or
                CLAIM_TTL_DAYS is not a positive whole number.
        """
        source = os.environ if env is None else env
        missing = [
            name for name in REQUIRED_ENV_VARS if not source.get(name, "").strip()
        ]
        if missing:
            msg = (
                f"Missing required environment variables: {', '.join(sorted(missing))}"
            )
            raise ConfigurationError(msg)

        # AWS_REGION is set by the Lambda runtime and is a reserved key, so it is
        # never declared in the function configuration.
        region = source.get("AWS_REGION", "").strip()
        if not region:
            msg = "AWS_REGION is not set; the runtime is expected to provide it"
            raise ConfigurationError(msg)

        raw_ttl_days = source["CLAIM_TTL_DAYS"].strip()
        try:
            claim_ttl_days = int(raw_ttl_days)
        except ValueError as exc:
            msg = f"CLAIM_TTL_DAYS must be a whole number of days, got {raw_ttl_days!r}"
            raise ConfigurationError(msg) from exc
        if claim_ttl_days <= 0:
            msg = f"CLAIM_TTL_DAYS must be greater than zero, got {claim_ttl_days}"
            raise ConfigurationError(msg)

        error_prefix = source["ERROR_PREFIX"].strip()
        if not error_prefix.endswith("/"):
            error_prefix = f"{error_prefix}/"

        return cls(
            config_table=source["CONFIG_TABLE_NAME"].strip(),
            state_table=source["STATE_TABLE_NAME"].strip(),
            event_bus=source["ALERT_EVENT_BUS_NAME"].strip(),
            alert_event_source=source["ALERT_EVENT_SOURCE"].strip(),
            catalog_uri=source["ICEBERG_CATALOG_URI"].strip(),
            warehouse=source["ICEBERG_WAREHOUSE"].strip(),
            error_prefix=error_prefix,
            metrics_namespace=source["METRICS_NAMESPACE"].strip(),
            claim_ttl_days=claim_ttl_days,
            region=region,
        )


@dataclass(frozen=True)
class ObjectRef:
    """One delivered object, as the notification named it."""

    bucket: str
    key: str
    version_id: str | None

    @property
    def relative_key(self) -> str:
        """The key with the raw root removed, used to build error-prefix keys."""
        return self.key.removeprefix(RAW_ROOT)

    @property
    def claim_version(self) -> str:
        """The version the claim is keyed on, or the unversioned stand-in."""
        return self.version_id or UNVERSIONED


@dataclass(frozen=True)
class ObjectPath:
    """The source and type a dropped object's key resolves to."""

    source: str
    type: str
    filename: str
    relative_key: str
    """The key with the raw/ root removed, reused to build the error-prefix key."""


def parse_object_key(key: str) -> ObjectPath | None:
    """Resolve a landing-bucket key to its source and type.

    Args:
        key: Object key, already URL-decoded.

    Returns:
        The parsed path, or None when the key does not match the agreed shape.
    """
    match = OBJECT_KEY_PATTERN.match(key)
    if match is None:
        return None
    return ObjectPath(
        source=match["source"],
        type=match["type"],
        filename=match["filename"],
        relative_key=key.removeprefix(RAW_ROOT),
    )


@dataclass(frozen=True)
class FieldSpec:
    """One field of the declarative mapping."""

    source_name: str
    target_name: str
    field_type: FieldType
    timestamp_format: TimestampFormat | None = None
    scale: Decimal | None = None


@dataclass(frozen=True)
class EntitySpec:
    """Which mapped field identifies the thing a reading is about.

    The entity partition key is ``{type}#{value}``, which is what the snapshot
    item, the alert-state items and the alert rules are all keyed on.
    """

    type: str
    key_field: str


@dataclass(frozen=True)
class Range:
    """Inclusive bounds a value must fall within."""

    minimum: Decimal | None
    maximum: Decimal | None


@dataclass(frozen=True)
class MappingConfig:
    """Everything the engine needs to turn one source's files into rows."""

    source: str
    type: str
    input_format: InputFormat
    has_header: bool
    fields: tuple[FieldSpec, ...]
    required: tuple[str, ...]
    ranges: Mapping[str, Range]
    target_table: str
    entity: EntitySpec


def load_mapping_config(
    client: DynamoDBClient,
    table_name: str,
    source: str,
    type_name: str,
) -> MappingConfig | None:
    """Fetch and parse the mapping config for a source and type.

    Args:
        client: DynamoDB client.
        table_name: Config table name.
        source: Source segment from the object key.
        type_name: Type segment from the object key.

    Returns:
        The parsed config, or None when no config item exists.

    Raises:
        MappingConfigError: An item exists but does not describe a usable
            mapping.
    """
    response = client.get_item(
        TableName=table_name,
        Key={
            "pk": {"S": f"config#{source}#{type_name}"},
            "sk": {"S": MAPPING_CONFIG_SORT_KEY},
        },
        ConsistentRead=False,
    )
    if "Item" not in response:
        return None
    return parse_mapping_config(
        _plain(response["Item"]), source=source, type_name=type_name
    )


def parse_mapping_config(
    item: Mapping[str, Any],
    *,
    source: str,
    type_name: str,
) -> MappingConfig:
    """Turn a config item into the typed mapping the engine works from.

    Args:
        item: Config item with DynamoDB attribute types already removed.
        source: Source segment from the object key.
        type_name: Type segment from the object key.

    Returns:
        The parsed config.

    Raises:
        MappingConfigError: Any part of the item is absent or unusable.
    """
    unit = {"source": source, "type": type_name}
    input_format = _enum_member(
        InputFormat, item.get("input_format"), "input_format", unit
    )

    raw_fields = item.get("fields")
    if not isinstance(raw_fields, list) or not raw_fields:
        msg = "Mapping config declares no fields"
        raise MappingConfigError(msg, unit=unit)
    fields = tuple(_parse_field(raw_field, unit) for raw_field in raw_fields)

    target_table = item.get("target_table")
    if not isinstance(target_table, str) or not target_table.strip():
        msg = "Mapping config declares no target_table"
        raise MappingConfigError(msg, unit=unit)

    validation = item.get("validation") or {}
    if not isinstance(validation, dict):
        msg = "Mapping config validation block is not an object"
        raise MappingConfigError(msg, unit=unit)

    known_targets = {field.target_name for field in fields}
    required = tuple(_parse_required(validation.get("required"), known_targets, unit))
    ranges = _parse_ranges(validation.get("ranges"), known_targets, unit)

    return MappingConfig(
        source=source,
        type=type_name,
        input_format=input_format,
        has_header=bool(item.get("has_header", True)),
        fields=fields,
        required=required,
        ranges=ranges,
        target_table=target_table.strip(),
        entity=_parse_entity(item.get("entity"), known_targets, unit),
    )


def _parse_field(raw: object, unit: Mapping[str, object]) -> FieldSpec:
    if not isinstance(raw, dict):
        msg = "Mapping config field entry is not an object"
        raise MappingConfigError(msg, unit=unit)
    source_name = raw.get("from")
    target_name = raw.get("to")
    if not isinstance(source_name, str) or not isinstance(target_name, str):
        msg = "Mapping config field entry needs a 'from' and a 'to' name"
        raise MappingConfigError(msg, unit=unit)

    field_type = _enum_member(FieldType, raw.get("type"), "field type", unit)
    timestamp_format: TimestampFormat | None = None
    if field_type is FieldType.TIMESTAMP:
        timestamp_format = _enum_member(
            TimestampFormat, raw.get("format"), "timestamp format", unit
        )

    scale: Decimal | None = None
    if "scale" in raw and raw["scale"] is not None:
        try:
            scale = Decimal(str(raw["scale"]))
        except InvalidOperation as exc:
            msg = f"Mapping config scale for {target_name} is not a number"
            raise MappingConfigError(msg, unit=unit) from exc

    return FieldSpec(
        source_name=source_name,
        target_name=target_name,
        field_type=field_type,
        timestamp_format=timestamp_format,
        scale=scale,
    )


def _parse_required(
    raw: object,
    known_targets: set[str],
    unit: Mapping[str, object],
) -> Sequence[str]:
    if raw is None:
        return ()
    if not isinstance(raw, list) or any(not isinstance(name, str) for name in raw):
        msg = "Mapping config validation.required is not a list of field names"
        raise MappingConfigError(msg, unit=unit)
    unknown = sorted(set(raw) - known_targets)
    if unknown:
        msg = f"validation.required names unmapped fields: {', '.join(unknown)}"
        raise MappingConfigError(msg, unit=unit)
    return [str(name) for name in raw]


def _parse_ranges(
    raw: object,
    known_targets: set[str],
    unit: Mapping[str, object],
) -> Mapping[str, Range]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        msg = "Mapping config validation.ranges is not an object"
        raise MappingConfigError(msg, unit=unit)
    unknown = sorted(set(raw) - known_targets)
    if unknown:
        msg = f"validation.ranges names unmapped fields: {', '.join(unknown)}"
        raise MappingConfigError(msg, unit=unit)

    ranges: dict[str, Range] = {}
    for name, bounds in raw.items():
        if not isinstance(bounds, dict):
            msg = f"validation.ranges entry for {name} is not an object"
            raise MappingConfigError(msg, unit=unit)
        ranges[str(name)] = Range(
            minimum=_optional_decimal(bounds.get("min"), f"{name}.min", unit),
            maximum=_optional_decimal(bounds.get("max"), f"{name}.max", unit),
        )
    return ranges


def _parse_entity(
    raw: object,
    known_targets: set[str],
    unit: Mapping[str, object],
) -> EntitySpec:
    if not isinstance(raw, dict):
        msg = "Mapping config declares no entity block"
        raise MappingConfigError(msg, unit=unit)
    entity_type = raw.get("type")
    key_field = raw.get("key_field")
    if not isinstance(entity_type, str) or not entity_type.strip():
        msg = "Mapping config entity block declares no type"
        raise MappingConfigError(msg, unit=unit)
    if not isinstance(key_field, str) or key_field not in known_targets:
        msg = "Mapping config entity.key_field must name a mapped field"
        raise MappingConfigError(msg, unit=unit)
    return EntitySpec(type=entity_type.strip(), key_field=key_field)


def _optional_decimal(
    value: object,
    label: str,
    unit: Mapping[str, object],
) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation as exc:
        msg = f"Mapping config {label} is not a number"
        raise MappingConfigError(msg, unit=unit) from exc


def _enum_member[T: StrEnum](
    enum_type: type[T],
    value: object,
    label: str,
    unit: Mapping[str, object],
) -> T:
    if isinstance(value, str):
        try:
            return enum_type(value.lower())
        except ValueError as exc:
            allowed = ", ".join(member.value for member in enum_type)
            msg = f"Mapping config {label} {value!r} is not one of: {allowed}"
            raise MappingConfigError(msg, unit=unit) from exc
    allowed = ", ".join(member.value for member in enum_type)
    msg = f"Mapping config {label} is missing; expected one of: {allowed}"
    raise MappingConfigError(msg, unit=unit)


DESERIALIZER = TypeDeserializer()


def _plain(item: Mapping[str, Any]) -> dict[str, Any]:
    """Strip DynamoDB attribute-type wrappers from an item."""
    return {key: DESERIALIZER.deserialize(value) for key, value in item.items()}
