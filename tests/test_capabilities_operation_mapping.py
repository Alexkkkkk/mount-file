"""Sync tests: canonical <-> API operation mapping must round-trip.

Keeps the capability gate contract in sync with the code: for every
interface registered in ``_INTERFACE_OPERATION_ALIASES`` the mapping
canonical -> API operation -> canonical must be an identity round-trip.
If a developer renames an operation without updating both directions,
these tests fail immediately.
"""

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from mountfile.capabilities import (
    _INTERFACE_OPERATION_ALIASES,
    api_operation_for_canonical_operation,
    canonical_operation_for_api_operation,
)

pytestmark = pytest.mark.unit

interface_strategy = st.sampled_from(sorted(_INTERFACE_OPERATION_ALIASES))
HEALTH = [HealthCheck.too_slow]


class TestOperationMappingRoundTrip:
    @settings(max_examples=100, deadline=None, suppress_health_check=HEALTH)
    @given(data=st.data())
    def test_canonical_to_api_to_canonical_round_trips(self, data):
        interface = data.draw(interface_strategy)
        canonical_ops = sorted(_INTERFACE_OPERATION_ALIASES[interface])
        canonical = data.draw(st.sampled_from(canonical_ops))
        api_op = api_operation_for_canonical_operation(interface, canonical)
        assert api_op is not None
        assert canonical_operation_for_api_operation(interface, api_op) == canonical

    def test_every_registered_operation_maps_both_ways(self):
        for interface, aliases in _INTERFACE_OPERATION_ALIASES.items():
            for canonical, api_op in aliases.items():
                assert api_operation_for_canonical_operation(interface, canonical) == api_op
                assert canonical_operation_for_api_operation(interface, api_op) == canonical

    def test_unknown_interface_returns_none(self):
        assert canonical_operation_for_api_operation("no-such-interface", "list") is None
        assert api_operation_for_canonical_operation("no-such-interface", "list") is None

    def test_unknown_operation_returns_none(self):
        assert canonical_operation_for_api_operation("vfs", "no-such-op") is None
        assert api_operation_for_canonical_operation("vfs", "no-such-op") is None
