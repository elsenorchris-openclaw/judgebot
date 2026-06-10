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


@pytest.fixture(autouse=True)
def _disable_forecast_anchor_for_suite():
    """2026-06-03: _window_peak_hour() forecast-anchors the gate's peak/min hour,
    which would hit OpenMeteo (network) during unit tests and make gate/clock tests
    that mock _lookup_peak_hour use the forecast instead. Default forecast-anchor OFF
    for the suite so _window_peak_hour returns the (mocked) empirical hour; the
    dedicated forecast tests (test_forecast_anchor) re-enable it and mock the fetch."""
    try:
        import config
    except Exception:
        yield
        return
    with mock.patch.object(config, "FORECAST_ANCHOR_ENABLED", False):
        yield


@pytest.fixture(autouse=True)
def _disable_irrev_lock_only_for_gate_tests():
    """2026-06-10: the IRREVERSIBLE-LOCK-ONLY mode (Gate -1) blocks every
    unlocked BUY before it reaches the downstream gates, which is the whole
    point in prod but would short-circuit the legacy gate tests (they build
    unlocked packets to exercise SPECIFIC gates: thin-margin, sigma, tail-bet,
    off-peak, window, caps...). Same pattern as BLEND_ONLY_EXECUTION above:
    default OFF for the suite; the dedicated test_irrev_lock_only re-enables it
    explicitly inside its own mock.patch."""
    try:
        import config
    except Exception:
        yield
        return
    with mock.patch.object(config, "PUSH_IRREV_LOCK_ONLY", False):
        yield


@pytest.fixture(autouse=True)
def _disable_wont_reach_gate_for_gate_tests():
    """2026-06-10: the HIGH won't-reach NO veto (mu < floor-0.5) blocks the
    mu-below-bracket packets many legacy gate tests use to exercise OTHER
    gates. Same pattern as BLEND_ONLY_EXECUTION above: default OFF for the
    suite; test_wont_reach_gate re-enables it explicitly."""
    try:
        import config
    except Exception:
        yield
        return
    with mock.patch.object(config, "PUSH_HIGH_NO_SKIP_WONT_REACH", False):
        yield


@pytest.fixture(autouse=True)
def _disable_climate_day_guard_for_suite():
    """2026-06-04: _in_decision_window refuses brackets whose climate_day != the
    station's current wall-clock date. The window/clock gate tests pass FIXED past
    climate_days (e.g. 2026-06-03), which this guard would reject as "not today".
    Default it OFF for the suite so those tests exercise their target gate; the
    dedicated test_climate_day_guard re-enables it and mocks _station_local_date."""
    try:
        import config
    except Exception:
        yield
        return
    with mock.patch.object(config, "CLIMATE_DAY_GUARD_ENABLED", False):
        yield
