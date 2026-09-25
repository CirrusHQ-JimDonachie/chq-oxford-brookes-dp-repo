"""Machine-readable rejection reason codes.

The values are a closed set. They are stamped on every record and file written
to the error prefix, and used as the rejection-reason dimension on the
rejected-record metric, so adding, renaming or removing one changes both the
alarm's series and the contract data owners triage against.
"""

from __future__ import annotations

from enum import StrEnum


class ReasonCode(StrEnum):
    """Why a record or a whole file was rejected."""

    MALFORMED_KEY = "MALFORMED_KEY"
    """The object key is not raw/{source}/{type}/yyyy/mm/dd/{filename}."""

    NO_MAPPING_CONFIG = "NO_MAPPING_CONFIG"
    """No mapping config exists for the source and type in the key."""

    INVALID_MAPPING_CONFIG = "INVALID_MAPPING_CONFIG"
    """The mapping config exists but does not describe a usable mapping."""

    UNPARSEABLE_FILE = "UNPARSEABLE_FILE"
    """The object could not be decoded or parsed as its declared format."""

    MISSING_REQUIRED_FIELD = "MISSING_REQUIRED_FIELD"
    """A field the config lists as required is absent or empty in the record."""

    TYPE_COERCION_FAILED = "TYPE_COERCION_FAILED"
    """A value could not be read as the type the config declares for it."""

    VALUE_OUT_OF_RANGE = "VALUE_OUT_OF_RANGE"
    """A value fell outside the min or max the config declares for the field."""
