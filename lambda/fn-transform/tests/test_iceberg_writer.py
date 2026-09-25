"""Target-table resolution, schema derivation, creation, evolution and append."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Self

import pyarrow as pa
import pytest
from config import FieldType, parse_mapping_config
from exceptions import CatalogWriteError
from iceberg_writer import (
    ARROW_TYPES,
    ICEBERG_TYPES,
    INGESTED_AT_COLUMN,
    SOURCE_FILE_COLUMN,
    IcebergTableWriter,
    arrow_schema,
    arrow_value,
    provenance,
    table_identifier,
)

from conftest import mapping_config_item

if TYPE_CHECKING:
    from collections.abc import Sequence

NOW = datetime(2026, 6, 25, 10, 5, 41, tzinfo=UTC)
ARROW_FOR_ICEBERG = {str(ICEBERG_TYPES[kind]): ARROW_TYPES[kind] for kind in FieldType}

ROW = {
    "room_id": "NHHB-2.14",
    "reading_at": datetime(2025, 6, 25, 10, 5, tzinfo=UTC),
    "temperature_c": Decimal("26.2"),
    "humidity_pct": Decimal(55),
}


class FakeSchema:
    """The part of an Iceberg table schema this writer reads."""

    def __init__(self, schema: pa.Schema) -> None:
        """Wrap an Arrow schema."""
        self._schema = schema

    def as_arrow(self) -> pa.Schema:
        """Return the Arrow form the append is shaped against."""
        return self._schema


class FakeUpdate:
    """Collects added columns the way PyIceberg's schema update does."""

    def __init__(self, table: FakeTable) -> None:
        """Hold the table the update applies to."""
        self._table = table

    def __enter__(self) -> Self:
        """Start the update."""
        return self

    def __exit__(self, *_: object) -> None:
        """Commit the update."""
        return

    def add_column(self, name: str, field_type: object, *_: object) -> None:
        """Append the column to the table's schema."""
        self._table.added_columns.append(name)
        self._table.arrow_schema = self._table.arrow_schema.append(
            pa.field(name, ARROW_FOR_ICEBERG[str(field_type)])
        )


class FakeTable:
    """An Iceberg table that records appends instead of writing Parquet."""

    def __init__(self, schema: pa.Schema) -> None:
        """Start from the schema the table was created with."""
        self.arrow_schema = schema
        self.appended: list[pa.Table] = []
        self.added_columns: list[str] = []

    def schema(self) -> FakeSchema:
        """Return the table's current schema."""
        return FakeSchema(self.arrow_schema)

    def update_schema(self) -> FakeUpdate:
        """Start a schema update."""
        return FakeUpdate(self)

    def append(self, df: pa.Table) -> None:
        """Record the append."""
        self.appended.append(df)


class FakeCatalog:
    """A REST catalog stand-in holding tables in memory."""

    def __init__(self, tables: dict[tuple[str, str], FakeTable] | None = None) -> None:
        """Start with the given tables, if any."""
        self.tables = tables or {}
        self.created: list[tuple[str, str]] = []

    def table_exists(self, identifier: tuple[str, str]) -> bool:
        """Whether the table is already in the catalog."""
        return identifier in self.tables

    def create_table(self, identifier: tuple[str, str], schema: pa.Schema) -> FakeTable:
        """Create the table from an Arrow schema."""
        self.created.append(identifier)
        self.tables[identifier] = FakeTable(schema)
        return self.tables[identifier]

    def load_table(self, identifier: tuple[str, str]) -> FakeTable:
        """Return an existing table."""
        return self.tables[identifier]


def build_writer(catalog: FakeCatalog) -> IcebergTableWriter:
    """A writer wired to a fake catalog rather than the REST endpoint."""
    return IcebergTableWriter(
        catalog_uri="https://glue.eu-west-2.amazonaws.com/iceberg",
        warehouse="123456789012:s3tablescatalog/obu-curated",
        region="eu-west-2",
        loader=lambda *_args, **_kwargs: catalog,  # type: ignore[arg-type]  # fake stands in for Catalog
    )


def config_fields() -> Sequence[Any]:
    """The worked example's mapped fields."""
    return parse_mapping_config(
        mapping_config_item(), source="bms", type_name="environmental"
    ).fields


def rows() -> list[dict[str, object]]:
    """One mapped row with its provenance columns."""
    return [{**ROW, **provenance("raw/bms/environmental/2025/06/25/nhhb-env.csv", NOW)}]


def test_table_identifier_reads_a_plain_namespace_and_table() -> None:
    """The common config form is the namespace and table on their own."""
    assert table_identifier("building_data.bms_environmental") == (
        "building_data",
        "bms_environmental",
    )


def test_table_identifier_reduces_a_fully_qualified_path_to_its_last_two_parts() -> (
    None
):
    """The warehouse already binds the catalog, so the prefix is not addressable."""
    assert table_identifier("s3tablescatalog/obu-curated/building_data/bms_env") == (
        "building_data",
        "bms_env",
    )


def test_table_identifier_raises_when_no_namespace_is_named() -> None:
    """A bare table name has no namespace for the catalog to look in."""
    with pytest.raises(CatalogWriteError, match="namespace"):
        table_identifier("bms_environmental")


def test_arrow_schema_follows_the_configured_field_types() -> None:
    """The curated column types come from the config, not from the first file."""
    schema = arrow_schema(config_fields())
    assert schema.field("room_id").type == pa.string()
    assert schema.field("temperature_c").type == pa.float64()
    assert schema.field("reading_at").type == pa.timestamp("us", tz="UTC")


def test_arrow_schema_adds_the_provenance_columns() -> None:
    """Every curated row says which file it came from and when it landed."""
    names = arrow_schema(config_fields()).names
    assert names[-2:] == [SOURCE_FILE_COLUMN, INGESTED_AT_COLUMN]


def test_arrow_value_converts_a_decimal_reading_to_the_curated_double() -> None:
    """The scaled reading is carried as a Decimal until the column type is applied."""
    assert arrow_value(Decimal("26.2")) == pytest.approx(26.2)


def test_write_creates_the_target_table_when_it_is_absent() -> None:
    """A new dataset is onboarded by writing a config row, not by a deployment."""
    catalog = FakeCatalog()
    build_writer(catalog).write("building_data.bms_env", config_fields(), rows())
    assert catalog.created == [("building_data", "bms_env")]


def test_write_appends_the_mapped_rows_to_the_table() -> None:
    """One append per dropped file, carrying every surviving record."""
    catalog = FakeCatalog()
    build_writer(catalog).write("building_data.bms_env", config_fields(), rows())
    appended = catalog.tables["building_data", "bms_env"].appended[0]
    assert appended.num_rows == 1
    assert appended.column("temperature_c")[0].as_py() == pytest.approx(26.2)
    assert appended.column("room_id")[0].as_py() == "NHHB-2.14"


def test_write_adds_a_column_the_config_declares_that_the_table_lacks() -> None:
    """A source that starts reporting a new field evolves the table in place."""
    existing = FakeTable(
        pa.schema(
            [
                pa.field("room_id", pa.string()),
                pa.field("reading_at", pa.timestamp("us", tz="UTC")),
                pa.field("temperature_c", pa.float64()),
                pa.field(SOURCE_FILE_COLUMN, pa.string()),
                pa.field(INGESTED_AT_COLUMN, pa.timestamp("us", tz="UTC")),
            ]
        )
    )
    catalog = FakeCatalog({("building_data", "bms_env"): existing})
    build_writer(catalog).write("building_data.bms_env", config_fields(), rows())
    assert existing.added_columns == ["humidity_pct"]
    assert existing.appended[0].column("humidity_pct")[0].as_py() == pytest.approx(55.0)


def test_write_leaves_an_up_to_date_table_alone() -> None:
    """A table that already matches the config is appended to, not altered."""
    catalog = FakeCatalog()
    writer = build_writer(catalog)
    writer.write("building_data.bms_env", config_fields(), rows())
    writer.write("building_data.bms_env", config_fields(), rows())
    table = catalog.tables["building_data", "bms_env"]
    assert table.added_columns == []
    assert len(table.appended) == 2


def test_write_does_nothing_when_every_record_was_rejected() -> None:
    """A file with no surviving rows costs no catalog call at all."""
    catalog = FakeCatalog()
    build_writer(catalog).write("building_data.bms_env", config_fields(), [])
    assert catalog.created == []


def test_write_raises_catalog_write_error_when_the_catalog_refuses() -> None:
    """A catalog failure is a platform failure and must reach the queue."""

    class RefusingCatalog(FakeCatalog):
        def table_exists(self, identifier: tuple[str, str]) -> bool:
            """Fail the way an unreachable endpoint would."""
            del identifier
            msg = "endpoint unreachable"
            raise RuntimeError(msg)

    with pytest.raises(CatalogWriteError, match="bms_env"):
        build_writer(RefusingCatalog()).write(
            "building_data.bms_env", config_fields(), rows()
        )


def test_catalog_is_loaded_once_and_reused_across_invocations() -> None:
    """The signed connection is worth keeping for the warm execution environment."""
    calls: list[str] = []

    def loader(_name: str, **properties: str) -> FakeCatalog:
        calls.append(properties["uri"])
        return FakeCatalog()

    writer = IcebergTableWriter(
        catalog_uri="https://glue.eu-west-2.amazonaws.com/iceberg",
        warehouse="123456789012:s3tablescatalog/obu-curated",
        region="eu-west-2",
        loader=loader,  # type: ignore[arg-type]  # fake stands in for Catalog
    )
    writer.catalog()
    writer.catalog()
    assert calls == ["https://glue.eu-west-2.amazonaws.com/iceberg"]


def test_catalog_is_loaded_with_the_sigv4_properties_the_endpoint_requires() -> None:
    """Without sigv4 and the glue signing name the endpoint rejects every call."""
    captured: dict[str, str] = {}

    def loader(_name: str, **properties: str) -> FakeCatalog:
        captured.update(properties)
        return FakeCatalog()

    IcebergTableWriter(
        catalog_uri="https://glue.eu-west-2.amazonaws.com/iceberg",
        warehouse="123456789012:s3tablescatalog/obu-curated",
        region="eu-west-2",
        loader=loader,  # type: ignore[arg-type]  # fake stands in for Catalog
    ).catalog()
    assert captured["type"] == "rest"
    assert captured["rest.sigv4-enabled"] == "true"
    assert captured["rest.signing-name"] == "glue"
    assert captured["rest.signing-region"] == "eu-west-2"
    assert captured["warehouse"] == "123456789012:s3tablescatalog/obu-curated"
