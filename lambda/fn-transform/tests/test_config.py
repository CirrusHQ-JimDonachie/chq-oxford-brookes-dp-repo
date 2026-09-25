"""Settings validation, object-key parsing and mapping-config parsing."""

from __future__ import annotations

from decimal import Decimal

import pytest
from config import (
    FieldType,
    InputFormat,
    Settings,
    TimestampFormat,
    parse_mapping_config,
    parse_object_key,
)
from exceptions import ConfigurationError, MappingConfigError

from conftest import ENVIRONMENT, mapping_config_item


def test_settings_raises_configuration_error_when_a_variable_is_missing() -> None:
    """A missing variable is named rather than surfacing later as a KeyError."""
    env = {
        key: value for key, value in ENVIRONMENT.items() if key != "STATE_TABLE_NAME"
    }
    with pytest.raises(ConfigurationError, match="STATE_TABLE_NAME"):
        Settings.from_env(env)


def test_settings_treats_an_empty_variable_as_missing() -> None:
    """An empty string is not a usable table name."""
    env = {**ENVIRONMENT, "CONFIG_TABLE_NAME": "   "}
    with pytest.raises(ConfigurationError, match="CONFIG_TABLE_NAME"):
        Settings.from_env(env)


def test_settings_rejects_a_claim_ttl_that_is_not_a_number() -> None:
    """A non-numeric day count fails at init rather than at the first claim."""
    env = {**ENVIRONMENT, "CLAIM_TTL_DAYS": "fortnight"}
    with pytest.raises(ConfigurationError, match="CLAIM_TTL_DAYS"):
        Settings.from_env(env)


def test_settings_rejects_a_claim_ttl_of_zero() -> None:
    """A zero-day claim would expire before the retry it exists to stop."""
    env = {**ENVIRONMENT, "CLAIM_TTL_DAYS": "0"}
    with pytest.raises(ConfigurationError, match="greater than zero"):
        Settings.from_env(env)


def test_settings_appends_a_slash_to_an_error_prefix_without_one() -> None:
    """The prefix is joined to keys directly, so it has to end in a separator."""
    settings = Settings.from_env({**ENVIRONMENT, "ERROR_PREFIX": "error"})
    assert settings.error_prefix == "error/"


def test_settings_raises_when_the_runtime_region_is_absent() -> None:
    """Requests to the REST catalog cannot be signed without a region."""
    env = {key: value for key, value in ENVIRONMENT.items() if key != "AWS_REGION"}
    with pytest.raises(ConfigurationError, match="AWS_REGION"):
        Settings.from_env(env)


def test_parse_object_key_returns_source_and_type_for_an_agreed_key() -> None:
    """The source and type segments are what the config lookup keys on."""
    path = parse_object_key("raw/bms/environmental/2026/06/25/nhhb-env.csv")
    assert path is not None
    assert (path.source, path.type) == ("bms", "environmental")
    assert path.relative_key == "bms/environmental/2026/06/25/nhhb-env.csv"


def test_parse_object_key_returns_none_for_a_key_outside_the_agreed_shape() -> None:
    """A key with no date path cannot be resolved to a source and type."""
    assert parse_object_key("raw/bms/nhhb-env.csv") is None


def test_parse_object_key_returns_none_for_a_key_outside_the_raw_prefix() -> None:
    """Only the raw prefix carries dropped files."""
    assert parse_object_key("error/bms/environmental/2026/06/25/nhhb-env.csv") is None


def test_parse_mapping_config_reads_the_worked_example() -> None:
    """The documented config item maps to the typed config the engine uses."""
    config = parse_mapping_config(
        mapping_config_item(), source="bms", type_name="environmental"
    )
    assert config.input_format is InputFormat.CSV
    assert config.entity.type == "room"
    assert config.entity.key_field == "room_id"
    assert config.required == ("room_id", "reading_at")
    assert config.ranges["temperature_c"].maximum == Decimal(60)
    temperature = next(f for f in config.fields if f.target_name == "temperature_c")
    assert temperature.field_type is FieldType.DECIMAL
    assert temperature.scale == Decimal("0.1")
    reading_at = next(f for f in config.fields if f.target_name == "reading_at")
    assert reading_at.timestamp_format is TimestampFormat.EPOCH_MS


def test_parse_mapping_config_rejects_an_unknown_input_format() -> None:
    """A format the engine cannot read is a config error, not a code gap."""
    item = {**mapping_config_item(), "input_format": "parquet"}
    with pytest.raises(MappingConfigError, match="input_format"):
        parse_mapping_config(item, source="bms", type_name="environmental")


def test_parse_mapping_config_rejects_a_timestamp_field_with_no_format() -> None:
    """Without a declared encoding an epoch value cannot be read."""
    item = mapping_config_item()
    item["fields"][1].pop("format")
    with pytest.raises(MappingConfigError, match="timestamp format"):
        parse_mapping_config(item, source="bms", type_name="environmental")


def test_parse_mapping_config_rejects_a_missing_entity_block() -> None:
    """Without an entity the snapshot and the alert rules have no key."""
    item = {
        key: value for key, value in mapping_config_item().items() if key != "entity"
    }
    with pytest.raises(MappingConfigError, match="entity"):
        parse_mapping_config(item, source="bms", type_name="environmental")


def test_parse_mapping_config_rejects_an_entity_key_that_is_not_mapped() -> None:
    """An entity field the mapping never produces would key every item on None."""
    item = {**mapping_config_item(), "entity": {"type": "room", "key_field": "site_id"}}
    with pytest.raises(MappingConfigError, match=r"entity\.key_field"):
        parse_mapping_config(item, source="bms", type_name="environmental")


def test_parse_mapping_config_rejects_a_required_field_that_is_not_mapped() -> None:
    """A required field that cannot exist would reject every record silently."""
    item = mapping_config_item()
    item["validation"]["required"] = ["room_id", "occupancy"]
    with pytest.raises(MappingConfigError, match="occupancy"):
        parse_mapping_config(item, source="bms", type_name="environmental")


def test_parse_mapping_config_rejects_an_empty_field_list() -> None:
    """A config with no fields describes no mapping at all."""
    item = {**mapping_config_item(), "fields": []}
    with pytest.raises(MappingConfigError, match="no fields"):
        parse_mapping_config(item, source="bms", type_name="environmental")
