"""Verify the session-scoped shared cache is built exactly once per run.

The ``shared_probe_cache`` fixture (see ``conftest.py``) uses a file lock so
that under pytest-xdist only the first worker builds the resource and the
rest reuse it — instead of rebuilding it once per worker.
"""

import pytest

pytestmark = pytest.mark.unit


def test_shared_probe_cache_reports_single_build(shared_probe_cache):
    assert shared_probe_cache["built_once"] is True


def test_shared_probe_cache_payload_is_stable(shared_probe_cache):
    assert set(shared_probe_cache) == {"built_once", "by"}
