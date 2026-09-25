"""Reading a dropped file and mapping its records per the declarative config.

Parsing, renaming, type coercion, scaling and validation all come from the
mapping config. There is no hook for per-source code: a file the config cannot
express is a rejection.
"""

from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING

from config import FieldType, InputFormat, TimestampFormat
from exceptions import FileFormatError
from reasons import ReasonCode

if TYPE_CHECKING:
    from collections.abc import Mapping

    from config import FieldSpec, MappingConfig

TRUE_TOKENS = frozenset({"true", "t", "yes", "y", "1", "on"})
FALSE_TOKENS = frozenset({"false", "f", "no", "n", "0", "off"})

MAX_DETAIL_LENGTH = 256
"""Rejection detail is user-controlled text, so it is capped and stripped."""


@dataclass(frozen=True)
class Rejection:
    """One record that will not reach the curated table."""

    reason: ReasonCode
    detail: str
    record_index: int
    record: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class RawRecord:
    """One record as it was read from the file, before any mapping."""

    index: int
    values: Mapping[str, str]
    rejection: Rejection | None = None
    """Set when the record itself could not be read, for example a bad JSON line."""


def parse_records(body: bytes, config: MappingConfig) -> list[RawRecord]:
    """Read a file's bytes into raw records per the config's declared format.

    Args:
        body: The object's bytes.
        config: The mapping config for this source and type.

    Returns:
        Raw records in file order. A record that could not be read carries its
        own rejection rather than aborting the file.

    Raises:
        FileFormatError: The bytes could not be decoded, or the file as a whole
            is not the declared format.
    """
    try:
        text = body.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        msg = "Object is not valid UTF-8 text"
        raise FileFormatError(msg) from exc

    match config.input_format:
        case InputFormat.CSV:
            return _parse_csv(text, config)
        case InputFormat.JSON:
            return _parse_json(text)
        case InputFormat.JSONL:
            return _parse_jsonl(text)


def _parse_csv(text: str, config: MappingConfig) -> list[RawRecord]:
    if config.has_header:
        reader = csv.DictReader(io.StringIO(text))
        return [
            RawRecord(index=index, values=_as_text(row))
            for index, row in enumerate(reader)
        ]

    # Without a header row the config's field order is the column order.
    column_names = [spec.source_name for spec in config.fields]
    records: list[RawRecord] = []
    for index, row in enumerate(csv.reader(io.StringIO(text))):
        values = dict(zip(column_names, row, strict=False))
        records.append(RawRecord(index=index, values=values))
    return records


def _parse_json(text: str) -> list[RawRecord]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        msg = f"Object is not valid JSON: {exc.msg}"
        raise FileFormatError(msg) from exc
    if not isinstance(payload, list):
        msg = "JSON input must be an array of objects"
        raise FileFormatError(msg)

    records: list[RawRecord] = []
    for index, entry in enumerate(payload):
        if isinstance(entry, dict):
            records.append(RawRecord(index=index, values=_as_text(entry)))
        else:
            records.append(
                _unreadable(index, repr(entry), "Array entry is not an object")
            )
    return records


def _parse_jsonl(text: str) -> list[RawRecord]:
    records: list[RawRecord] = []
    index = 0
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError as exc:
            records.append(
                _unreadable(index, line, f"Line is not valid JSON: {exc.msg}")
            )
        else:
            if isinstance(entry, dict):
                records.append(RawRecord(index=index, values=_as_text(entry)))
            else:
                records.append(_unreadable(index, line, "Line is not a JSON object"))
        index += 1
    return records


def _unreadable(index: int, raw_line: str, detail: str) -> RawRecord:
    record = {"raw": _capped(raw_line)}
    return RawRecord(
        index=index,
        values={},
        rejection=Rejection(
            reason=ReasonCode.UNPARSEABLE_FILE,
            detail=detail,
            record_index=index,
            record=record,
        ),
    )


def map_record(raw: RawRecord, config: MappingConfig) -> dict[str, object] | Rejection:
    """Rename, coerce and validate one record.

    Args:
        raw: The record as read from the file.
        config: The mapping config for this source and type.

    Returns:
        The mapped values keyed by target name, or the rejection explaining why
        the record will not be written.
    """
    if raw.rejection is not None:
        return raw.rejection

    values: dict[str, object] = {}
    for spec in config.fields:
        text = raw.values.get(spec.source_name)
        if text is None or not text.strip():
            values[spec.target_name] = None
            continue
        try:
            values[spec.target_name] = _coerce(text.strip(), spec)
        except ValueError as exc:
            return Rejection(
                reason=ReasonCode.TYPE_COERCION_FAILED,
                detail=_capped(f"{spec.target_name}: {exc}"),
                record_index=raw.index,
                record=raw.values,
            )

    return _validate(values, raw, config)


def _validate(
    values: dict[str, object],
    raw: RawRecord,
    config: MappingConfig,
) -> dict[str, object] | Rejection:
    for name in config.required:
        if values.get(name) is None:
            return Rejection(
                reason=ReasonCode.MISSING_REQUIRED_FIELD,
                detail=_capped(f"{name} is required and was absent or empty"),
                record_index=raw.index,
                record=raw.values,
            )

    for name, bounds in config.ranges.items():
        value = values.get(name)
        if value is None:
            continue
        number = _as_number(value)
        if number is None:
            return Rejection(
                reason=ReasonCode.VALUE_OUT_OF_RANGE,
                detail=_capped(f"{name} has a range rule but is not numeric"),
                record_index=raw.index,
                record=raw.values,
            )
        below = bounds.minimum is not None and number < bounds.minimum
        above = bounds.maximum is not None and number > bounds.maximum
        if below or above:
            return Rejection(
                reason=ReasonCode.VALUE_OUT_OF_RANGE,
                detail=_capped(
                    f"{name} value {number} is outside its configured range"
                ),
                record_index=raw.index,
                record=raw.values,
            )

    return values


def _coerce(text: str, spec: FieldSpec) -> object:
    match spec.field_type:
        case FieldType.STRING:
            return text
        case FieldType.INTEGER:
            return _to_integer(text, spec)
        case FieldType.DECIMAL:
            return _to_decimal(text, spec)
        case FieldType.BOOLEAN:
            return _to_boolean(text)
        case FieldType.TIMESTAMP:
            return _to_timestamp(text, spec)


def _to_integer(text: str, spec: FieldSpec) -> int:
    try:
        value = int(text)
    except ValueError as exc:
        msg = f"{text!r} is not a whole number"
        raise ValueError(msg) from exc
    if spec.scale is not None:
        scaled = Decimal(value) * spec.scale
        if scaled != scaled.to_integral_value():
            msg = f"{text!r} scaled by {spec.scale} is not a whole number"
            raise ValueError(msg)
        return int(scaled)
    return value


def _to_decimal(text: str, spec: FieldSpec) -> Decimal:
    try:
        value = Decimal(text)
    except InvalidOperation as exc:
        msg = f"{text!r} is not a number"
        raise ValueError(msg) from exc
    if not value.is_finite():
        msg = f"{text!r} is not a finite number"
        raise ValueError(msg)
    return value * spec.scale if spec.scale is not None else value


def _to_boolean(text: str) -> bool:
    lowered = text.lower()
    if lowered in TRUE_TOKENS:
        return True
    if lowered in FALSE_TOKENS:
        return False
    msg = f"{text!r} is not a recognised true or false value"
    raise ValueError(msg)


def _to_timestamp(text: str, spec: FieldSpec) -> datetime:
    match spec.timestamp_format:
        case TimestampFormat.EPOCH_MS | TimestampFormat.EPOCH_S:
            return _from_epoch(text, spec.timestamp_format)
        case TimestampFormat.ISO8601:
            return _from_iso8601(text)
        case None:  # pragma: no cover - the config parser rejects this first
            msg = "timestamp field has no declared format"
            raise ValueError(msg)


def _from_epoch(text: str, timestamp_format: TimestampFormat) -> datetime:
    try:
        raw = Decimal(text)
    except InvalidOperation as exc:
        msg = f"{text!r} is not an epoch value"
        raise ValueError(msg) from exc
    seconds = raw / 1000 if timestamp_format is TimestampFormat.EPOCH_MS else raw
    try:
        return datetime.fromtimestamp(float(seconds), tz=UTC)
    except (OverflowError, OSError, ValueError) as exc:
        msg = f"{text!r} is not a representable timestamp"
        raise ValueError(msg) from exc


def _from_iso8601(text: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        msg = f"{text!r} is not an ISO-8601 timestamp"
        raise ValueError(msg) from exc
    return (
        parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
    )


def _as_number(value: object) -> Decimal | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    return None


def _as_text(row: Mapping[str, object]) -> dict[str, str]:
    return {
        str(key): "" if value is None else str(value)
        for key, value in row.items()
        if key is not None
    }


def _capped(text: str) -> str:
    collapsed = " ".join(text.split())
    if len(collapsed) <= MAX_DETAIL_LENGTH:
        return collapsed
    return f"{collapsed[:MAX_DETAIL_LENGTH]}..."


def entity_key(config: MappingConfig, values: Mapping[str, object]) -> str | None:
    """Build the partition key the snapshot, rules and alert state share.

    Args:
        config: The mapping config for this source and type.
        values: One record's mapped values.

    Returns:
        ``{entity type}#{entity value}``, or None when the record carries no
        entity value.
    """
    raw = values.get(config.entity.key_field)
    if raw is None:
        return None
    text = str(raw).strip()
    return f"{config.entity.type}#{text}" if text else None
