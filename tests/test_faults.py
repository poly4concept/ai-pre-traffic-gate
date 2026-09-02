"""Tests for demo-app fault injection. Phase 2.5.

Two clusters carry the weight:

  * fail SAFE -- an unreadable or malformed config must leave the app healthy,
    which is the opposite of every other fail-* rule in this project
  * the fault actually fires -- an injected error has to reach CloudWatch's
    `Errors` metric, or every synthesized health scenario is silently a no-op

A NOTE ON THE IMPORT AT THE TOP

It is a plain `import faults`, not `pytest.importorskip("faults")`. The first
version of this file used importorskip, `services/demo_app` was not on sys.path,
and all twenty-two tests were silently SKIPPED -- reported by pytest as a clean
run with no failures.

Which is this project's own thesis committed against itself: an absent result
presented as a passing one. `importorskip` is right for a genuinely optional
dependency and wrong for a module that must exist, because it converts "this is
broken" into "this did not run", and the two look identical in the output.
"""

from __future__ import annotations

import random

import faults
import pytest
from conftest import load_handler


@pytest.fixture(autouse=True)
def _clean_module_state():
    faults.reset_for_tests()
    yield
    faults.reset_for_tests()


class FakeTable:
    def __init__(self, item=None, raises=None):
        self.item = item
        self.raises = raises
        self.calls = 0

    def get_item(self, **kwargs):
        self.calls += 1
        self.last_kwargs = kwargs
        if self.raises:
            raise self.raises
        return {"Item": self.item} if self.item else {}


def record(**overrides):
    item = {
        "error_rate": {"N": "0"},
        "latency_ms": {"N": "0"},
        "memory_mb": {"N": "0"},
    }
    item.update(overrides)
    return item


# --- Fail SAFE, not closed ------------------------------------------------


def test_an_unreadable_config_injects_nothing(monkeypatch):
    """The opposite of the gate, and deliberately so.

    A demo app that breaks when its config store is unavailable is
    indistinguishable from the fault we are trying to inject -- you could not
    tell a real outage from a synthetic one, which destroys the experiment the
    app exists to support.
    """
    monkeypatch.setattr(faults, "FAULT_TABLE", "t")
    fake = FakeTable(raises=RuntimeError("AccessDeniedException"))

    config = faults.load_faults(client=fake, use_cache=False)

    assert config is faults.NO_FAULTS
    assert not config.is_active


def test_no_table_configured_injects_nothing(monkeypatch):
    monkeypatch.setattr(faults, "FAULT_TABLE", "")

    assert faults.load_faults(use_cache=False) is faults.NO_FAULTS


def test_a_missing_record_injects_nothing(monkeypatch):
    monkeypatch.setattr(faults, "FAULT_TABLE", "t")

    config = faults.load_faults(client=FakeTable(item=None), use_cache=False)

    assert not config.is_active


@pytest.mark.parametrize(
    "item",
    [
        {"error_rate": {"S": "lots"}},
        {"error_rate": {"N": "not-a-number"}},
        {"latency_ms": {"N": ""}},
        {},
    ],
)
def test_a_malformed_record_injects_nothing(item):
    """An unparseable config is one to ignore, not a reason to break the app."""
    assert not faults.parse_record(item).is_active


# --- Clamping -------------------------------------------------------------


def test_values_are_clamped_to_their_ceilings():
    config = faults.parse_record(
        record(
            error_rate={"N": "9"},
            latency_ms={"N": "999999"},
            memory_mb={"N": "99999"},
        )
    )

    assert config.error_rate == faults.MAX_ERROR_RATE
    assert config.latency_ms == faults.MAX_LATENCY_MS
    assert config.memory_mb == faults.MAX_MEMORY_MB


def test_negative_values_are_floored_at_zero():
    config = faults.parse_record(record(error_rate={"N": "-1"}, latency_ms={"N": "-500"}))

    assert config.error_rate == 0
    assert config.latency_ms == 0


def test_the_latency_ceiling_is_above_the_function_timeout():
    """Producing a genuine Lambda timeout is a scenario we want.

    Clamping below the 5s timeout would make it unreachable, and "the function
    timed out" is a distinct and useful target health signal.
    """
    assert faults.MAX_LATENCY_MS > 5_000


# --- The fault actually fires ---------------------------------------------


def test_an_error_fault_raises_rather_than_returning():
    """CloudWatch's `Errors` metric counts unhandled exceptions, not statuses.

    A handler that caught its own fault and returned 500 would be a complete
    success as far as Lambda is concerned: Errors stays at zero, no alarm fires,
    and the gate reads a 0% error rate on a service where every request fails.
    That is the absent-versus-zero trap, arriving from the other direction.
    """
    config = faults.FaultConfig(error_rate=1.0)

    with pytest.raises(faults.InjectedFault):
        faults.apply_faults(config, "1", rng=random.Random(0))


def test_a_zero_error_rate_never_raises():
    config = faults.FaultConfig(error_rate=0.0, latency_ms=1)

    applied = faults.apply_faults(config, "1", sleep=lambda _: None)

    assert "failed" not in applied


def test_the_error_rate_is_actually_a_rate():
    """Roughly half of a large sample, not all or nothing."""
    config = faults.FaultConfig(error_rate=0.5)
    rng = random.Random(1234)
    failures = 0

    for _ in range(400):
        try:
            faults.apply_faults(config, "1", rng=rng)
        except faults.InjectedFault:
            failures += 1

    assert 150 < failures < 250


def test_latency_is_applied_before_the_error_roll():
    """A request that is about to fail should still have been slow.

    Otherwise the latency metric quietly excludes exactly the requests that were
    degraded most, and p99 looks better the worse the service gets.
    """
    slept = []
    config = faults.FaultConfig(error_rate=1.0, latency_ms=2000)

    with pytest.raises(faults.InjectedFault):
        faults.apply_faults(config, "1", rng=random.Random(0), sleep=slept.append)

    assert slept == [2.0]


def test_an_inactive_config_does_nothing():
    applied = faults.apply_faults(faults.NO_FAULTS, "1")

    assert applied == {"injected": False}


# --- Version scoping ------------------------------------------------------


def test_a_version_scoped_fault_spares_other_versions():
    """The canary demo: break the new version, leave the stable one healthy.

    This is the scenario CodeDeploy's automatic rollback exists for.
    """
    config = faults.FaultConfig(error_rate=1.0, applies_to_version="7")

    assert config.applies_to("7")
    assert not config.applies_to("6")

    spared = faults.apply_faults(config, "6", rng=random.Random(0))
    assert spared == {"injected": False}

    with pytest.raises(faults.InjectedFault):
        faults.apply_faults(config, "7", rng=random.Random(0))


def test_version_comparison_survives_int_versus_string():
    """Lambda version numbers arrive as strings; a config may hold either."""
    assert faults.FaultConfig(error_rate=1.0, applies_to_version="7").applies_to(7)


def test_an_unscoped_fault_applies_everywhere():
    config = faults.FaultConfig(error_rate=1.0, applies_to_version=None)

    assert config.applies_to("1")
    assert config.applies_to("99")


# --- Caching --------------------------------------------------------------


def test_the_config_is_cached_between_invocations(monkeypatch):
    monkeypatch.setattr(faults, "FAULT_TABLE", "t")
    fake = FakeTable(item=record(error_rate={"N": "0.5"}))
    clock = iter([0.0, 1.0, 2.0])

    first = faults.load_faults(client=fake, now=lambda: next(clock))
    second = faults.load_faults(client=fake, now=lambda: next(clock))

    assert first == second
    assert fake.calls == 1


def test_the_cache_expires(monkeypatch):
    monkeypatch.setattr(faults, "FAULT_TABLE", "t")
    monkeypatch.setattr(faults, "CACHE_TTL_SECONDS", 5)
    fake = FakeTable(item=record(error_rate={"N": "0.5"}))
    times = iter([0.0, 100.0, 100.0])

    faults.load_faults(client=fake, now=lambda: next(times))
    faults.load_faults(client=fake, now=lambda: next(times))

    assert fake.calls == 2


def test_the_read_is_strongly_consistent(monkeypatch):
    """Faults are switched on seconds before a demo.

    An eventually-consistent read can serve the previous value, which is a
    genuinely confusing thing to debug in front of an audience.
    """
    monkeypatch.setattr(faults, "FAULT_TABLE", "t")
    fake = FakeTable(item=record())

    faults.load_faults(client=fake, use_cache=False)

    assert fake.last_kwargs["ConsistentRead"] is True


# --- Memory ---------------------------------------------------------------


def test_memory_ballast_is_held_across_invocations():
    """A local would be collected the moment the handler returned.

    Module-level state is the only way to actually grow a container's footprint,
    and without it the memory-pressure scenario silently does nothing.
    """
    faults._grow_memory(4)
    assert len(faults._ballast) == 4

    faults._grow_memory(4)
    assert len(faults._ballast) == 4, "repeat calls must not keep allocating"

    faults._grow_memory(1)
    assert len(faults._ballast) == 1, "a lower target must release"


# --- Through the handler --------------------------------------------------


def test_the_handler_serves_normally_when_no_faults_are_set(monkeypatch):
    handler = load_handler("demo_app")
    monkeypatch.setattr(handler, "load_faults", lambda: faults.NO_FAULTS)

    class Ctx:
        function_version = "3"
        aws_request_id = "r"

    response = handler.lambda_handler({}, Ctx())

    assert response["statusCode"] == 200
    import json

    assert json.loads(response["body"])["faults"] == {"injected": False}


def test_the_handler_lets_an_injected_fault_propagate(monkeypatch):
    """It must reach the runtime to be counted as a Lambda error."""
    handler = load_handler("demo_app")
    monkeypatch.setattr(handler, "load_faults", lambda: faults.FaultConfig(error_rate=1.0))

    class Ctx:
        function_version = "3"
        aws_request_id = "r"

    with pytest.raises(faults.InjectedFault):
        handler.lambda_handler({}, Ctx())


def test_a_config_read_failure_does_not_break_the_handler(monkeypatch):
    handler = load_handler("demo_app")

    def boom():
        raise RuntimeError("dynamodb is having a day")

    monkeypatch.setattr(handler, "load_faults", boom)

    class Ctx:
        function_version = "3"
        aws_request_id = "r"

    assert handler.lambda_handler({}, Ctx())["statusCode"] == 200
