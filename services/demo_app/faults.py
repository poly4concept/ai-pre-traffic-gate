"""Controllable fault injection for the demo app. Phase 2.5.

WHY THIS EXISTS

CLAUDE.md draws a hard line between two kinds of signal:

    MOCKED           hardcoded JSON from a collector's mock implementation.
                     Unblocks development. Never touches AWS.
    SYNTHESIZED-REAL conditions engineered in the account so real AWS services
                     genuinely emit real findings, which the real collectors
                     then parse.

Both are required. Mocked signals alone hide parsing bugs and produce a demo
that cannot be honestly defended on stage. This module is how the second kind
gets manufactured: turn the demo app unhealthy on demand, let CloudWatch
actually observe it, and let the gate read a target that is genuinely broken
right now.

WHY THE CONFIG LIVES IN DYNAMODB AND NOT IN AN ENVIRONMENT VARIABLE

This is the non-obvious part, and it is worth understanding before it costs
somebody an afternoon.

The obvious design is an environment variable and
`aws lambda update-function-configuration`. It does not work here, and it fails
*silently*.

Publishing a Lambda version snapshots the code AND the configuration, env vars
included. `update-function-configuration` modifies `$LATEST` only; already
published versions keep the snapshot they were born with. The `live` alias
points at published versions -- that is the whole basis of the canary (D-010) --
so traffic served through the alias keeps the OLD environment.

The result: you set `ERROR_RATE=0.5`, the CLI reports success, and absolutely
nothing changes for any request an actual user makes. No error, no warning.

A DynamoDB record is read at invoke time by whichever version is running, so it
applies to the stable version and the canary alike -- and, via
`applies_to_version`, to exactly one of them when that is what you want.

WHY THIS FAILS SAFE WHEN THE GATE FAILS CLOSED

If the config cannot be read, no faults are injected.

That is the opposite of the decision service, and deliberately so. A demo app
that breaks when its config store is unavailable would be indistinguishable from
the fault we are trying to inject -- you could not tell a real outage from a
synthetic one, which destroys the experiment the app exists to support.

The general point, which is talk material: "fail closed" is not a virtue in
itself. It is the correct default for a component that DECIDES something. This
component only performs, and the safe default for a performer is to behave
normally.
"""

from __future__ import annotations

import logging
import os
import random
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

FAULT_TABLE = os.environ.get("FAULT_TABLE", "")
FAULT_KEY = os.environ.get("FAULT_KEY", "demo-app")

# How long a container reuses a config read before going back to DynamoDB.
#
# The trade: at 0 every invocation pays a read; at 60 a demo feels broken for a
# minute after you switch it off. Five seconds keeps the stage demo responsive
# while making the read rate irrelevant to cost. Cold containers pick up changes
# immediately, warm ones within the window -- so a fault appearing to "linger"
# on some requests and not others is expected, not a bug.
CACHE_TTL_SECONDS = float(os.environ.get("FAULT_CACHE_TTL", "5"))

# Hard ceilings, applied after reading the record.
#
# The record is operator-controlled rather than attacker-controlled, so these are
# not a security boundary -- they exist so a typo costs cents rather than a
# capped Lambda concurrency and a surprising bill. MAX_LATENCY_MS sits above the
# function's 5s timeout on purpose: producing a genuine timeout is a scenario we
# want, and clamping below the timeout would make it unreachable.
MAX_ERROR_RATE = 1.0
MAX_LATENCY_MS = 30_000
MAX_MEMORY_MB = 512


class InjectedFault(RuntimeError):
    """Raised to produce a deliberate, recognisable failure.

    A distinct type so that a synthetic error is never confused with a real one
    while reading logs -- the whole point is to know exactly which is which.
    """


@dataclass(frozen=True, slots=True)
class FaultConfig:
    """What to do to this invocation. All-zero means behave normally."""

    error_rate: float = 0.0
    latency_ms: int = 0
    memory_mb: int = 0
    # None means every version. Set to a published version number to break the
    # canary while leaving the stable version healthy -- which is the scenario
    # CodeDeploy's automatic rollback exists for, and the one worth showing on
    # stage.
    applies_to_version: str | None = None
    note: str = ""

    @property
    def is_active(self) -> bool:
        return bool(self.error_rate or self.latency_ms or self.memory_mb)

    def applies_to(self, version: str) -> bool:
        if not self.is_active:
            return False
        if self.applies_to_version is None:
            return True
        return str(self.applies_to_version) == str(version)

    def to_dict(self) -> dict[str, Any]:
        return {
            "error_rate": self.error_rate,
            "latency_ms": self.latency_ms,
            "memory_mb": self.memory_mb,
            "applies_to_version": self.applies_to_version,
            "note": self.note,
        }


NO_FAULTS = FaultConfig()

# Module-level so allocated memory survives across invocations in the same
# container -- which is the only way to actually grow a Lambda's footprint.
# A local would be garbage collected the moment the handler returned, and the
# memory-pressure scenario would silently do nothing.
_ballast: list[bytes] = []

_cache: tuple[float, FaultConfig] | None = None
_client: Any = None


def _dynamodb() -> Any:
    """Built lazily so importing this module needs neither boto3 nor a region."""
    global _client
    if _client is None:
        import boto3

        _client = boto3.client("dynamodb")
    return _client


def _as_number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def parse_record(item: dict[str, Any]) -> FaultConfig:
    """Turn a raw DynamoDB item into a clamped FaultConfig.

    Separated from the read so the clamping can be tested without a client, and
    so a malformed record produces NO_FAULTS rather than an exception -- an
    unparseable config is a config we should ignore, not a reason to break the
    app we are trying to control.
    """
    try:
        error_rate = min(
            max(_as_number(item.get("error_rate", {}).get("N", 0)), 0.0), MAX_ERROR_RATE
        )
        latency_ms = int(
            min(max(_as_number(item.get("latency_ms", {}).get("N", 0)), 0), MAX_LATENCY_MS)
        )
        memory_mb = int(
            min(max(_as_number(item.get("memory_mb", {}).get("N", 0)), 0), MAX_MEMORY_MB)
        )
        version = item.get("applies_to_version", {}).get("S") or None
        note = item.get("note", {}).get("S", "")
    except (AttributeError, TypeError) as exc:
        logger.warning("fault record is malformed; injecting nothing: %s", exc)
        return NO_FAULTS

    return FaultConfig(
        error_rate=error_rate,
        latency_ms=latency_ms,
        memory_mb=memory_mb,
        applies_to_version=version,
        note=note,
    )


def load_faults(*, client: Any = None, now: Any = None, use_cache: bool = True) -> FaultConfig:
    """Read the fault config. Never raises; returns NO_FAULTS on any problem."""
    global _cache

    clock = now or time.monotonic
    if use_cache and _cache is not None:
        cached_at, config = _cache
        if clock() - cached_at < CACHE_TTL_SECONDS:
            return config

    if not FAULT_TABLE:
        return NO_FAULTS

    try:
        response = (client or _dynamodb()).get_item(
            TableName=FAULT_TABLE,
            Key={"config_key": {"S": FAULT_KEY}},
            # Faults are switched on and off by hand seconds before a demo, and
            # an eventually-consistent read can serve the previous value. That is
            # a genuinely confusing failure on stage, and the read is one item.
            ConsistentRead=True,
        )
    except Exception as exc:  # noqa: BLE001 - see the module docstring: fail SAFE
        logger.warning("could not read fault config; injecting nothing: %s", exc)
        return NO_FAULTS

    config = parse_record(response.get("Item") or {})
    if use_cache:
        _cache = (clock(), config)
    return config


def apply_faults(
    config: FaultConfig,
    version: str,
    *,
    rng: random.Random | None = None,
    sleep: Any = time.sleep,
) -> dict[str, Any]:
    """Do what the config says. Returns what was done, or raises InjectedFault.

    The return value is echoed in the response and logged, so a demo can prove
    which requests were deliberately degraded rather than asking an audience to
    take it on trust.
    """
    applied: dict[str, Any] = {"injected": False}

    if not config.applies_to(version):
        return applied

    applied["injected"] = True
    applied["note"] = config.note

    if config.memory_mb:
        # Allocated before the delay and the error so that a request which is
        # about to fail still leaves its footprint behind -- memory pressure that
        # vanished whenever the fault fired would never build up.
        _grow_memory(config.memory_mb)
        applied["memory_mb"] = config.memory_mb

    if config.latency_ms:
        sleep(config.latency_ms / 1000.0)
        applied["latency_ms"] = config.latency_ms

    if config.error_rate:
        roll = (rng or random).random()
        applied["error_rate"] = config.error_rate
        if roll < config.error_rate:
            applied["failed"] = True
            raise InjectedFault(
                f"synthetic failure (error_rate={config.error_rate}, roll={roll:.3f})"
            )

    return applied


def _grow_memory(target_mb: int) -> None:
    """Hold roughly `target_mb` megabytes in this container.

    Idempotent: called repeatedly with the same target it allocates once. Called
    with a smaller target it releases, so the switch-off path actually frees the
    memory rather than requiring the container to be recycled.
    """
    global _ballast
    current_mb = len(_ballast)
    if current_mb == target_mb:
        return
    if current_mb > target_mb:
        del _ballast[target_mb:]
        return
    # bytearray rather than bytes: CPython interns and shares some byte objects,
    # and a shared allocation would not actually consume the memory we asked for.
    _ballast.extend(bytearray(1024 * 1024) for _ in range(target_mb - current_mb))


def reset_for_tests() -> None:
    """Clear module state. Tests only."""
    global _cache, _client
    _cache = None
    _client = None
    _ballast.clear()
