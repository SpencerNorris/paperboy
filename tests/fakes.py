"""Re-export `FakeGateway` under `tests.fakes` for collector/CLI tests.

`pyproject.toml` sets `pythonpath = ["."]`, which is what makes
`from tests.fakes import FakeGateway` resolve.
"""

from paperboy.gateway import FakeGateway

__all__ = ["FakeGateway"]
