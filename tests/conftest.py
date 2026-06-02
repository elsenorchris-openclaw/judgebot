"""Shared pytest fixtures for the judge bot test suite."""
import pytest
from unittest import mock


@pytest.fixture(autouse=True)
def _disable_blend_only_for_gate_tests():
    """The `_try_auto_execute` gate tests build packets with matcher / no
    `mu_method` to exercise SPECIFIC downstream gates (tier-1, thin-margin,
    tail-bet, sigma, etc.). The 2026-06-02 BLEND-ONLY gate (gate 0) would
    short-circuit all of them with "blend_only: mu_method=none". Default it OFF
    for the suite so those gate tests test their target gate; the dedicated
    blend-only test (test_blend_gate_changes.TestBlendOnlyExecution) re-enables
    it explicitly inside its own `mock.patch`. Tests that never read the flag
    (e.g. pure_nn_decide tests) are unaffected.
    """
    try:
        import config
    except Exception:
        yield
        return
    with mock.patch.object(config, "BLEND_ONLY_EXECUTION", False):
        yield
