"""Assembling the three signals into one normalized bundle.

Phase 2.1. The bundle is what Phase 3 turns into a prompt and what Phase 4
replays. Two properties matter more than anything else it does:

  1. It reports its own completeness. The verdict layer must be able to ask
     "how much of this did we actually manage to learn?" without inspecting
     each signal by hand -- because the answer changes whether a verdict should
     be issued at all.

  2. It serialises deterministically. Same input, same bytes. Constraint 6
     requires feeding a fixed scenario in and getting a comparable verdict out,
     which is impossible if the serialisation of identical signals differs
     between runs.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime
from enum import Enum
from typing import Any

from .base import SignalResult, SignalStatus
from .collectors import (
    ChangeContextCollector,
    SecurityFindingsCollector,
    TargetHealthCollector,
)
from .types import ChangeContext, DeploymentTarget, SecurityFindings, TargetHealth

# Signals the gate is not willing to decide without. Change context is the
# description of what is being deployed; without it there is literally no
# subject for a verdict, and any judgement would be about nothing.
#
# Security findings and target health are deliberately NOT required. Their
# absence degrades a verdict rather than preventing one, and treating every
# signal as mandatory would make the gate brittle enough that people route
# around it -- which is a worse security outcome than a verdict made with two
# signals out of three, honestly labelled.
REQUIRED_SIGNALS = frozenset({"change_context"})


@dataclass(frozen=True)
class SignalBundle:
    """Everything the gate knows, plus an honest account of what it does not."""

    target: DeploymentTarget
    collected_at: datetime

    change: SignalResult[ChangeContext]
    security: SignalResult[SecurityFindings]
    health: SignalResult[TargetHealth]

    @property
    def results(self) -> tuple[SignalResult[Any], ...]:
        return (self.change, self.security, self.health)

    @property
    def missing(self) -> tuple[str, ...]:
        """Collectors that produced no usable data, for any reason."""
        return tuple(r.collector for r in self.results if not r.is_usable)

    @property
    def failed(self) -> tuple[str, ...]:
        """Collectors that tried and could not. Excludes deliberate skips."""
        return tuple(r.collector for r in self.results if r.status is SignalStatus.UNAVAILABLE)

    @property
    def is_complete(self) -> bool:
        return not self.missing

    @property
    def has_required_signals(self) -> bool:
        """Whether a verdict may be attempted at all.

        Phase 3 routes a False here straight to human review without calling
        Bedrock. Asking a model to assess a change it has not been told about
        would produce a fluent, confident, entirely baseless answer -- the
        single worst output this system could generate, because it is
        indistinguishable from a real verdict.
        """
        usable = {r.collector for r in self.results if r.is_usable}
        return REQUIRED_SIGNALS.issubset(usable)

    @property
    def completeness_summary(self) -> str:
        """One line for logs and for the prompt's own preamble.

        The verdict layer is told what it could not see. A model that knows
        health data is missing can say so in its reasoning; one that is handed
        a silently truncated bundle will reason confidently over a gap it has
        no way to detect.
        """
        usable = sum(1 for r in self.results if r.is_usable)
        total = len(self.results)
        if usable == total:
            return f"all {total} signals collected"
        return f"{usable}/{total} signals collected; missing: {', '.join(self.missing)}"

    def to_dict(self) -> dict[str, Any]:
        """Deterministic, JSON-safe representation for audit and replay."""
        return _plain(
            {
                "target": self.target,
                "collected_at": self.collected_at,
                "signals": {
                    r.collector: {
                        "status": r.status,
                        "data": r.data,
                        "error": r.error,
                        # duration_ms is deliberately EXCLUDED. It varies run to run
                        # on identical inputs, so including it would break the
                        # byte-identical replay that constraint 6 depends on. It is
                        # still logged separately -- it is an operational metric,
                        # not part of the evidence.
                    }
                    for r in self.results
                },
                "completeness": {
                    "is_complete": self.is_complete,
                    "has_required_signals": self.has_required_signals,
                    "missing": list(self.missing),
                    "failed": list(self.failed),
                },
            }
        )


def _plain(value: Any) -> Any:
    """Recursively convert to JSON-safe primitives, deterministically."""
    if is_dataclass(value) and not isinstance(value, type):
        return _plain(asdict(value))
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        # Sorted so two bundles with identical content serialise identically
        # regardless of insertion order.
        return {k: _plain(v) for k, v in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value


def collect_signals(
    target: DeploymentTarget,
    change_collector: ChangeContextCollector,
    security_collector: SecurityFindingsCollector,
    health_collector: TargetHealthCollector,
    now: datetime | None = None,
) -> SignalBundle:
    """Run all three collectors and assemble the bundle.

    Collectors are injected rather than constructed here. That is what lets the
    same function run against mocks in a unit test, against fixtures in the
    Phase 4 eval harness, and against real AWS in production, with no branching
    on an `is_mock` flag anywhere in the code path being tested.

    `now` is injectable for the same reason: a bundle that stamps itself with
    `datetime.now()` cannot be reproduced, and constraint 6 requires that
    feeding a fixed scenario in twice produces comparable output.

    No collector can fail this function. Each `collect()` handles its own
    errors by contract, so a broken collector yields an UNAVAILABLE result and
    the bundle still assembles -- carrying an accurate description of what is
    missing rather than throwing away the two signals that did work.
    """
    return SignalBundle(
        target=target,
        collected_at=now or datetime.now().astimezone(),
        change=change_collector.collect(),
        security=security_collector.collect(),
        health=health_collector.collect(),
    )
