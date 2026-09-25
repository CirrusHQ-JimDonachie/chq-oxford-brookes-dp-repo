"""Exception types raised by the transform engine.

Helpers raise these; the handler catches at the top of the invocation, logs the
unit of work carried on the exception, and re-raises so Lambda's async retry and
the dead-letter queue see the failure.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping


class TransformError(Exception):
    """Base class for every error this engine raises.

    Args:
        message: Human-readable description of the failure.
        unit: Identifiers of the unit of work the failure belongs to (bucket,
            object key, record index). The frame that catches is rarely the
            frame that holds these, so they travel on the exception and the
            failure log record names the same things the success record does.
    """

    def __init__(
        self, message: str, *, unit: Mapping[str, object] | None = None
    ) -> None:
        """Record the message and the unit of work the failure belongs to."""
        super().__init__(message)
        self.unit: Mapping[str, object] = dict(unit) if unit else {}


class ConfigurationError(TransformError):
    """A required environment variable is missing, empty or unusable."""


class MappingConfigError(TransformError):
    """The mapping config item exists but cannot be used as written."""


class FileFormatError(TransformError):
    """The dropped object cannot be decoded or parsed as its declared format."""


class ObjectReadError(TransformError):
    """The dropped object could not be read from the landing bucket."""


class CatalogWriteError(TransformError):
    """Creating, evolving or appending to the curated Iceberg table failed."""


class ObjectInFlightError(TransformError):
    """Another invocation is already processing this object version.

    The claim it holds is recent enough that its owner may still be running, so
    this delivery gives way. Lambda retries it, and an object that stays stuck
    reaches the dead-letter queue where a person can look at it.
    """


class DeadlineExceededError(TransformError):
    """Not enough invocation time remains to finish the object safely.

    Raised rather than returning a partial result. A half-written file would
    otherwise be marked done and the remainder lost with no signal; failing
    releases the claim, so the retry re-processes the object from the start.
    """
