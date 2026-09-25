"""File parsing, declarative field mapping and per-record validation."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from config import MappingConfig, parse_mapping_config
from exceptions import FileFormatError
from mapping import Rejection, entity_key, map_record, parse_records
from reasons import ReasonCode

from conftest import mapping_config_item

CSV_WITH_HEADER = b"SensorRef,Timestamp,RoomTemp,RH\nNHHB-2.14,1750845900000,262,55\n"


def build_config(**overrides: object) -> MappingConfig:
    """Parse the worked-example config with the given top-level overrides."""
    item = {**mapping_config_item(), **overrides}
    return parse_mapping_config(item, source="bms", type_name="environmental")


def test_parse_records_reads_a_csv_file_with_a_header_row() -> None:
    """Column names come from the header and match the config's from names."""
    records = parse_records(CSV_WITH_HEADER, build_config())
    assert len(records) == 1
    assert records[0].values["SensorRef"] == "NHHB-2.14"


def test_parse_records_maps_a_headerless_csv_by_the_configured_field_order() -> None:
    """With no header the config's field order is the only column order there is."""
    config = build_config(has_header=False)
    records = parse_records(b"NHHB-2.14,1750845900000,262,55\n", config)
    assert records[0].values["RoomTemp"] == "262"


def test_parse_records_reads_a_json_array_of_objects() -> None:
    """A JSON file is an array, one object per reading."""
    config = build_config(input_format="json")
    body = b'[{"SensorRef": "NHHB-2.14", "RoomTemp": 262}]'
    records = parse_records(body, config)
    assert records[0].values["RoomTemp"] == "262"


def test_parse_records_reads_json_lines() -> None:
    """Blank lines between records are skipped rather than counted."""
    config = build_config(input_format="jsonl")
    body = b'{"SensorRef": "A"}\n\n{"SensorRef": "B"}\n'
    records = parse_records(body, config)
    assert [record.values["SensorRef"] for record in records] == ["A", "B"]


def test_parse_records_rejects_only_the_bad_line_of_a_json_lines_file() -> None:
    """One malformed line does not cost the rest of the file."""
    config = build_config(input_format="jsonl")
    records = parse_records(b'{"SensorRef": "A"}\nnot json\n', config)
    assert records[0].rejection is None
    assert records[1].rejection is not None
    assert records[1].rejection.reason is ReasonCode.UNPARSEABLE_FILE


def test_parse_records_raises_format_error_for_a_json_object_at_top_level() -> None:
    """A single object is not the array shape the format declares."""
    config = build_config(input_format="json")
    with pytest.raises(FileFormatError, match="array of objects"):
        parse_records(b'{"SensorRef": "A"}', config)


def test_parse_records_raises_file_format_error_for_bytes_that_are_not_text() -> None:
    """An undecodable object is a whole-file problem, not a record one."""
    with pytest.raises(FileFormatError, match="UTF-8"):
        parse_records(b"\xff\xfe\x00binary", build_config())


def test_map_record_renames_coerces_and_scales_per_the_config() -> None:
    """The worked example's row becomes the canonical row the design shows."""
    record = parse_records(CSV_WITH_HEADER, build_config())[0]
    mapped = map_record(record, build_config())
    assert not isinstance(mapped, Rejection)
    assert mapped["room_id"] == "NHHB-2.14"
    assert mapped["temperature_c"] == Decimal("26.2")
    assert mapped["humidity_pct"] == Decimal(55)
    assert mapped["reading_at"] == datetime(2025, 6, 25, 10, 5, tzinfo=UTC)


def test_map_record_rejects_a_record_missing_a_required_field() -> None:
    """A reading with no room cannot be attributed to an entity."""
    config = build_config()
    record = parse_records(
        b"SensorRef,Timestamp,RoomTemp,RH\n,1750845900000,262,55\n", config
    )[0]
    mapped = map_record(record, config)
    assert isinstance(mapped, Rejection)
    assert mapped.reason is ReasonCode.MISSING_REQUIRED_FIELD
    assert "room_id" in mapped.detail


def test_map_record_rejects_a_value_that_will_not_coerce_to_its_type() -> None:
    """Text in a numeric column is a data problem with its own reason code."""
    config = build_config()
    body = b"SensorRef,Timestamp,RoomTemp,RH\nNHHB-2.14,1750845900000,warm,55\n"
    mapped = map_record(parse_records(body, config)[0], config)
    assert isinstance(mapped, Rejection)
    assert mapped.reason is ReasonCode.TYPE_COERCION_FAILED


def test_map_record_rejects_a_value_outside_its_configured_range() -> None:
    """The design's 700 degree reading is rejected after the scale is applied."""
    config = build_config()
    body = b"SensorRef,Timestamp,RoomTemp,RH\nNHHB-2.14,1750845900000,7000,55\n"
    mapped = map_record(parse_records(body, config)[0], config)
    assert isinstance(mapped, Rejection)
    assert mapped.reason is ReasonCode.VALUE_OUT_OF_RANGE


def test_map_record_keeps_an_optional_field_absent_rather_than_rejecting() -> None:
    """Humidity is not required, so a blank column is not a rejection."""
    config = build_config()
    body = b"SensorRef,Timestamp,RoomTemp,RH\nNHHB-2.14,1750845900000,262,\n"
    mapped = map_record(parse_records(body, config)[0], config)
    assert not isinstance(mapped, Rejection)
    assert mapped["humidity_pct"] is None


def test_map_record_carries_a_records_own_rejection_through_unchanged() -> None:
    """A line that never parsed is rejected as read, not mapped first."""
    config = build_config(input_format="jsonl")
    record = parse_records(b"not json\n", config)[0]
    mapped = map_record(record, config)
    assert isinstance(mapped, Rejection)
    assert mapped.reason is ReasonCode.UNPARSEABLE_FILE


@pytest.mark.parametrize(
    ("declared_format", "raw", "expected"),
    [
        ("epoch_ms", "1750845900000", datetime(2025, 6, 25, 10, 5, tzinfo=UTC)),
        ("epoch_s", "1750845900", datetime(2025, 6, 25, 10, 5, tzinfo=UTC)),
        ("iso8601", "2025-06-25T10:05:00Z", datetime(2025, 6, 25, 10, 5, tzinfo=UTC)),
    ],
)
def test_map_record_reads_each_supported_timestamp_format(
    declared_format: str,
    raw: str,
    expected: datetime,
) -> None:
    """Every declared encoding lands on the same instant in UTC."""
    item = mapping_config_item()
    item["fields"][1]["format"] = declared_format
    config = parse_mapping_config(item, source="bms", type_name="environmental")
    body = f"SensorRef,Timestamp,RoomTemp,RH\nNHHB-2.14,{raw},262,55\n".encode()
    mapped = map_record(parse_records(body, config)[0], config)
    assert not isinstance(mapped, Rejection)
    assert mapped["reading_at"] == expected


def test_map_record_reads_boolean_tokens_either_way_round() -> None:
    """Sources spell true and false several ways; the config does not have to."""
    item = mapping_config_item()
    item["fields"] = [
        {"from": "SensorRef", "to": "room_id", "type": "string"},
        {"from": "Occupied", "to": "occupied", "type": "boolean"},
    ]
    item["validation"] = {"required": ["room_id"], "ranges": {}}
    config = parse_mapping_config(item, source="bms", type_name="occupancy")
    mapped = map_record(
        parse_records(b"SensorRef,Occupied\nA,Yes\n", config)[0], config
    )
    assert not isinstance(mapped, Rejection)
    assert mapped["occupied"] is True


def test_entity_key_builds_the_partition_key_the_snapshot_and_rules_share() -> None:
    """The key shape is what ties a reading to its rules and its alert state."""
    assert entity_key(build_config(), {"room_id": "NHHB-2.14"}) == "room#NHHB-2.14"


def test_entity_key_returns_none_when_the_entity_value_is_absent() -> None:
    """A record with no entity has no snapshot and no rules to evaluate."""
    assert entity_key(build_config(), {"room_id": None}) is None
