"""Suite-wide guards."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from tests import fake_gcs


@pytest.fixture(scope="session", autouse=True)
def _never_deleted_from_a_bucket() -> Iterator[None]:
    """#63: paperboy has no delete path to a bucket; no test may reach one."""
    yield
    assert fake_gcs.DELETE_CALLS == [], "a test attempted to delete from a (fake) bucket"
