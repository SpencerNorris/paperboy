"""Suite-wide guards."""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

from tests import fake_gcs

# CLI tests assert on plain text. Rich reads FORCE_COLOR when the console is
# built at import time, so an operator's FORCE_COLOR would inject ANSI codes
# into every assertion; neutralise it before paperboy.cli is imported.
os.environ.pop("FORCE_COLOR", None)
os.environ["NO_COLOR"] = "1"


@pytest.fixture(scope="session", autouse=True)
def _never_deleted_from_a_bucket() -> Iterator[None]:
    """#63: paperboy has no delete path to a bucket; no test may reach one."""
    yield
    assert fake_gcs.DELETE_CALLS == [], "a test attempted to delete from a (fake) bucket"
