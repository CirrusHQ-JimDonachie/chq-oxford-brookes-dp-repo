"""Curated Iceberg writes through the AWS Glue Iceberg REST catalog endpoint.

One append per dropped file. The target table is created from the mapping
config's field list when it does not exist, and columns the config declares but
the table lacks are added before the append.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Any, Protocol

import pyarrow as pa
from config import FieldType
from exceptions import CatalogWriteError
from pyiceberg.catalog import load_catalog
from pyiceberg.types import (
    BooleanType,
    DoubleType,
    IcebergType,
    LongType,
    StringType,
    TimestamptzType,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from datetime import datetime

    from config import FieldSpec
    from pyiceberg.catalog import Catalog

SOURCE_FILE_COLUMN = "source_file"
INGESTED_AT_COLUMN = "ingested_at"

CATALOG_NAME = "s3tablescatalog"
"""Local name for the loaded catalog. The warehouse property is what binds it
to a table bucket, so this name is not itself an AWS identifier."""

ARROW_TYPES: dict[FieldType, pa.DataType] = {
    FieldType.STRING: pa.string(),
    FieldType.DECIMAL: pa.float64(),
    FieldType.INTEGER: pa.int64(),
    FieldType.BOOLEAN: pa.bool_(),
    FieldType.TIMESTAMP: pa.timestamp("us", tz="UTC"),
}

ICEBERG_TYPES: dict[FieldType, IcebergType] = {
    FieldType.STRING: StringType(),
    FieldType.DECIMAL: DoubleType(),
    FieldType.INTEGER: LongType(),
    FieldType.BOOLEAN: BooleanType(),
    FieldType.TIMESTAMP: TimestamptzType(),
}


class TableWriter(Protocol):
    """What the transform needs from whatever writes the curated rows."""

    def write(
        self,
        target_table: str,
        fields: Sequence[FieldSpec],
        rows: Sequence[Mapping[str, object]],
    ) -> None:
        """Append rows to the target table, creating or evolving it first."""


def table_identifier(target_table: str) -> tuple[str, str]:
    """Reduce a configured target table to the namespace and table it names.

    The warehouse property already binds the catalog to one table bucket, and
    Glue namespaces are single-level, so only the last two path components are
    addressable. Both ``building_data.bms_environmental`` and the fully
    qualified ``s3tablescatalog/bucket/building_data/bms_environmental`` a
    console shows therefore resolve to the same table.

    Args:
        target_table: The config's ``target_table`` value.

    Returns:
        The namespace and table name.

    Raises:
        CatalogWriteError: The value does not name a namespace and a table.
    """
    parts = [part for part in target_table.replace(".", "/").split("/") if part]
    if len(parts) < 2:  # noqa: PLR2004 - a namespace and a table name
        msg = f"target_table {target_table!r} does not name a namespace and a table"
        raise CatalogWriteError(msg)
    return parts[-2], parts[-1]


def arrow_schema(fields: Sequence[FieldSpec]) -> pa.Schema:
    """Build the Arrow schema a mapping config's field list describes.

    Args:
        fields: The config's mapped fields, in declared order.

    Returns:
        The schema, with the provenance columns appended.
    """
    columns = [
        pa.field(spec.target_name, ARROW_TYPES[spec.field_type]) for spec in fields
    ]
    columns.append(pa.field(SOURCE_FILE_COLUMN, pa.string()))
    columns.append(pa.field(INGESTED_AT_COLUMN, pa.timestamp("us", tz="UTC")))
    return pa.schema(columns)


def iceberg_columns(fields: Sequence[FieldSpec]) -> dict[str, IcebergType]:
    """Iceberg types for every column the config describes."""
    columns: dict[str, IcebergType] = {
        spec.target_name: ICEBERG_TYPES[spec.field_type] for spec in fields
    }
    columns[SOURCE_FILE_COLUMN] = StringType()
    columns[INGESTED_AT_COLUMN] = TimestamptzType()
    return columns


def arrow_value(value: object) -> object:
    """Convert a mapped value to something Arrow can hold.

    Decimals carry the scaled reading through validation and the DynamoDB
    write; the curated column is a double, so the conversion happens here
    rather than earlier where it would cost the comparison's exactness.
    """
    if isinstance(value, Decimal):
        return float(value)
    return value


class IcebergTableWriter:
    """Appends rows to S3 Tables through the Glue Iceberg REST endpoint.

    The catalog is resolved on first use and kept for the life of the execution
    environment, so a warm invocation reuses the signed connection.
    """

    def __init__(
        self,
        *,
        catalog_uri: str,
        warehouse: str,
        region: str,
        loader: Callable[..., Catalog] = load_catalog,
    ) -> None:
        """Hold the catalog properties without contacting the endpoint.

        Args:
            catalog_uri: The Glue Iceberg REST endpoint for the region.
            warehouse: ``{account id}:s3tablescatalog/{table bucket}``.
            region: Region the requests are signed for.
            loader: Catalog factory, replaced in tests.
        """
        self._catalog_uri = catalog_uri
        self._warehouse = warehouse
        self._region = region
        self._loader = loader
        self._catalog: Catalog | None = None

    def catalog(self) -> Catalog:
        """Resolve the REST catalog, reusing it across warm invocations."""
        if self._catalog is None:
            self._catalog = self._loader(
                CATALOG_NAME,
                **{
                    "type": "rest",
                    "uri": self._catalog_uri,
                    "warehouse": self._warehouse,
                    "rest.sigv4-enabled": "true",
                    "rest.signing-name": "glue",
                    "rest.signing-region": self._region,
                },
            )
        return self._catalog

    def write(
        self,
        target_table: str,
        fields: Sequence[FieldSpec],
        rows: Sequence[Mapping[str, object]],
    ) -> None:
        """Append rows to the target table, creating or evolving it first.

        Args:
            target_table: The config's ``target_table`` value.
            fields: The config's mapped fields, in declared order.
            rows: Mapped rows, already carrying the provenance columns.

        Raises:
            CatalogWriteError: The catalog rejected the create, the schema
                update or the append.
        """
        if not rows:
            return

        namespace, name = table_identifier(target_table)
        identifier = (namespace, name)
        catalog = self.catalog()

        try:
            if not catalog.table_exists(identifier):
                catalog.create_table(identifier=identifier, schema=arrow_schema(fields))
            table = catalog.load_table(identifier)
            _add_missing_columns(table, fields)
            table = catalog.load_table(identifier)
            table.append(df=self._arrow_table(table, rows))
        except CatalogWriteError:
            raise
        except Exception as exc:
            msg = f"Curated write to {namespace}.{name} failed: {exc}"
            raise CatalogWriteError(msg) from exc

    @staticmethod
    def _arrow_table(table: Any, rows: Sequence[Mapping[str, object]]) -> pa.Table:  # noqa: ANN401
        """Shape rows to the table's own schema so the append cannot mismatch.

        ``table`` is PyIceberg's Table, whose public surface this only reads;
        typing it precisely would pin the helper to one release of an
        unversioned import.
        """
        schema: pa.Schema = table.schema().as_arrow()
        names = set(schema.names)
        shaped = [
            {name: arrow_value(value) for name, value in row.items() if name in names}
            for row in rows
        ]
        return pa.Table.from_pylist(shaped, schema=schema)


def _add_missing_columns(table: Any, fields: Sequence[FieldSpec]) -> None:  # noqa: ANN401
    """Add columns the config declares that the table does not have yet."""
    existing = set(table.schema().as_arrow().names)
    missing = {
        name: field_type
        for name, field_type in iceberg_columns(fields).items()
        if name not in existing
    }
    if not missing:
        return
    with table.update_schema() as update:
        for name, field_type in missing.items():
            update.add_column(name, field_type)


def provenance(source_file: str, ingested_at: datetime) -> dict[str, object]:
    """The provenance columns stamped on every curated row."""
    return {SOURCE_FILE_COLUMN: source_file, INGESTED_AT_COLUMN: ingested_at}
