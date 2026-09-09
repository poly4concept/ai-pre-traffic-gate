"""The floor: facts the model is not allowed to argue with. Phase 5.5.

WHY THIS EXISTS, AND IT IS MEASURED RATHER THAN ASSUMED

Three scenarios in the eval set have failed on every model tried:

    critical_cve_no_patch          Haiku low, Sonnet low
    critical_cve_with_patch        Haiku low, Sonnet low
    untriaged_severity_findings    Haiku low, Sonnet low

Stably, three repeats each, same direction, and -- the part that settled it --
with the *same argument*: a routine dependency bump into a healthy target, with
the pre-existing finding discounted because this change did not introduce it.

That argument is not stupid. It is a coherent answer to a question we never
decided: does a gate flag every deploy to a service carrying an unpatchable
CVE, or only ones that make things worse? Both readings are defensible, no model
can pick between them on our behalf, and D-080 recorded the experiment proving a
more capable model reaches the identical conclusion.

WHAT A FLOOR IS, AND WHAT IT IS NOT

It is not a second opinion, and it does not overrule judgement. It sets the
LOWEST risk level the evidence permits, and the model is free to go higher.

    model says      floor says      result
    low             medium          medium      floor applied
    medium          medium          medium      no change
    high            medium          high        the model wins

That asymmetry is the whole design. Code owns the countable facts; the model
owns the judgement; and the model can add caution but never remove it. A control
the model could talk its way past would not be a control -- which matters more
than usual here, because the model's input includes a commit message written by
whoever is trying to deploy.

WHY THESE RULES AND NOT MORE

Only what is genuinely countable. "Is there a critical vulnerability in what I
am about to ship?" needs no judgement -- it is arithmetic, and Phase 4b already
showed fifteen lines of arithmetic beating a language model *specifically on
these scenarios*. Everything requiring interpretation -- is this refactor risky,
is this commit message manipulating me, is this a revert of a bad deploy --
stays with the model, because a floor cannot read.

Deliberately absent: any rule about target health. An active alarm is countable
too, and both models already handle it correctly, so a rule there would add
surface area and change nothing. A floor should encode the cases where the model
is measurably wrong, not every case where a rule is possible.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from signals import SignalBundle

from .types import RISK_ORDER, RiskLevel, Verdict

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Floor:
    """The minimum the evidence permits, and why."""

    level: RiskLevel
    reason: str


def required_floor(bundle: SignalBundle) -> Floor | None:
    """The lowest risk level this evidence allows, or None if unconstrained.

    Reads ONLY the security signal, and only its countable parts. Note what it
    does not do: it never looks at the commit message, the diff size, or
    anything else a change could describe about itself. A floor built on
    self-reported facts would be a floor an attacker could lower.
    """
    security = bundle.security.data
    if security is None:
        # No security data is not a floor. It is an ABSENT SIGNAL, and the
        # prompt already tells the model to treat absence as risk-raising
        # (D-026). Inventing a floor here would double-count the same fact and
        # push every unscanned deploy to medium on top of the model already
        # having done so.
        return None

    critical = security.critical_count
    if critical:
        return Floor(
            RiskLevel.MEDIUM,
            f"{critical} critical security finding(s) in the deployed artifact. "
            "A critical vulnerability in what is about to ship warrants a canary "
            "and a human glance regardless of who introduced it.",
        )

    unknown = security.unknown_count
    if unknown:
        # "We found things and cannot say how bad they are" is the absent-signal
        # principle inside a signal that was successfully collected -- the scan
        # ran, so the collector reports OK, and the severities are missing
        # anyway. Untriaged is not the same as harmless.
        return Floor(
            RiskLevel.MEDIUM,
            f"{unknown} security finding(s) of unknown severity. Untriaged is "
            "not the same as harmless, and nothing here establishes which it is.",
        )

    return None


def apply_floor(verdict: Verdict, bundle: SignalBundle) -> Verdict:
    """Raise a verdict to the floor if the evidence demands it. Never lowers.

    Returns the original object when nothing changes, so the common path
    allocates nothing and `is` comparison still works in tests.
    """
    floor = required_floor(bundle)
    if floor is None:
        return verdict

    if RISK_ORDER[verdict.risk_level] >= RISK_ORDER[floor.level]:
        # The model was already at least this cautious. Recorded at debug
        # because a floor that agreed with the model is not an event.
        logger.debug("floor %s not applied; model already said %s", floor.level, verdict.risk_level)
        return verdict

    logger.warning(
        "FLOOR APPLIED: model said %s, raised to %s -- %s",
        verdict.risk_level,
        floor.level,
        floor.reason,
    )
    return verdict.raised_to(floor.level, floor.reason)
