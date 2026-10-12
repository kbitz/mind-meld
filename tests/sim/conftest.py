"""Local fixture/report boundary. Root autouse isolation remains in force."""

import os

import pytest


def _lookup_write_guard(request):
    """Bind 71B's delivered shared-helper guard; never import root conftest."""
    from mind_meld import fsutil

    guard = getattr(fsutil, "refuse_real_state_write_under_pytest", None)
    if not callable(guard):
        return None
    probe = request.getfixturevalue("tmp_path") / "sim" / "guard-preflight"

    def verify():
        if not os.environ.get("PYTEST_CURRENT_TEST"):
            raise RuntimeError(
                "Cannot run simulator: Track 71B's guard needs PYTEST_CURRENT_TEST; "
                "run under pytest via ./bin/check; see README#prerequisites"
            )
        guard(probe)  # Read-only classification; no probe file is created.

    return verify


@pytest.fixture
def sim_write_guard(request):
    from .devices import MISSING_GUARD

    guard = _lookup_write_guard(request)
    if guard is None:
        pytest.fail(MISSING_GUARD, pytrace=False)
    guard()
    return guard


@pytest.fixture
def sim_fleet(tmp_path, sim_write_guard):
    from .devices import SimFleet

    yield SimFleet(tmp_path, write_guard=sim_write_guard).bootstrap()
    leaked = SimFleet.release_leaked()
    if leaked is not None:
        pytest.fail(
            f"Activation of {leaked.name} was never exited; its HOME and patches were "
            "undone at teardown. Use `with fleet.activate(...)`; see README#activation",
            pytrace=False,
        )


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    fleet = item.funcargs.get("sim_fleet")
    if report.failed and fleet is not None:
        try:
            diagnostics = fleet.diagnostics()
        except Exception as error:  # Never mask the test's own failure.
            diagnostics = f"diagnostics unavailable: {error!r}"
        report.sections.append(("sim_fleet reproduction", diagnostics))
