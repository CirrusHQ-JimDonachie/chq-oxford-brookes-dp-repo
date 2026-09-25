"""Fixtures shared by every handler's test suite in this directory."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator

    from aws_lambda_powertools.utilities.typing import LambdaContext


@pytest.fixture(autouse=True)
def aws_credentials() -> Iterator[None]:
    """Point boto3 at dummy credentials so no test can reach a real account.

    Autouse and set before any client is built, because a handler module
    creates its clients at import time.
    """
    dummy = {
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_SECURITY_TOKEN": "testing",
        "AWS_SESSION_TOKEN": "testing",
        "AWS_DEFAULT_REGION": "eu-west-2",
    }
    previous = {key: os.environ.get(key) for key in dummy}
    os.environ.update(dummy)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


@pytest.fixture
def lambda_context() -> LambdaContext:
    """A context object carrying the fields the handlers actually read."""

    class _Context:
        function_name = "test-function"
        memory_limit_in_mb = 512
        invoked_function_arn = (
            "arn:aws:lambda:eu-west-2:123456789012:function:test-function"
        )
        aws_request_id = "00000000-0000-0000-0000-000000000000"

        def get_remaining_time_in_millis(self) -> int:
            return 300_000

    context: Any = _Context()
    return context
