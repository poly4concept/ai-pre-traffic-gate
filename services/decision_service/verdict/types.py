"""What a verdict is. Phase 3.1.

This module defines the entire vocabulary the model is allowed to speak in, and
it is deliberately small. Three ideas are worth understanding before reading the
code, because each is a design decision that could plausibly have gone the other
way.

WHY RISK IS AN ENUM AND NOT A SCORE

A 0-100 risk score looks more informative and is worse. The executor has to
branch, so a score needs a threshold, and the threshold then becomes the real
policy -- sitting in a config file, unversioned, unexplained, and quietly doing
the actual deciding. Three named levels put the policy where a human can read
it. It also stops the model inventing "medium-high", which a free-text field
invites and an enum forbids.

WHY THE MODEL NEVER NAMES AN ACTION

Look at what is NOT in this vocabulary: there is no "deploy", no "halt", no
"proceed". The model describes risk; code maps risk to action. That mapping is
ACTION_FOR_RISK below -- four lines, fully tested, and not reachable by anything
the model emits.

This matters more than it looks. From Phase 2 on, the gate's input contains
commit messages and file paths, which on any repository accepting pull requests
is attacker-influenced text. If the model's output vocabulary included the word
"deploy", a successful prompt injection would need to produce exactly one token
to get what it wanted. Because the vocabulary is purely descriptive, the best an
injection can do is misdescribe the change -- and it still has to get past a
mapping it cannot influence, into an executor whose IAM role this process does
not hold. Restricting vocabulary is a cheap, structural mitigation, and it is
independent of whether the model behaves.

WHY A FAILED VERDICT HAS NO CONFIDENCE RATHER THAN ZERO CONFIDENCE

This is the Phase 2 invariant -- an absent signal is not a negative signal --
applied one layer up. When the gate fails closed it did not receive a confidence
value; it received nothing. Recording 0.0 would be a fabricated measurement, and
Phase 4 would then average it in alongside real ones. `confidence` is therefore
`float | None`, and `__post_init__` enforces that a model-sourced verdict must
carry one while a fail-closed verdict must not.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum


class RiskLevel(StrEnum):
    """The only three things the model may conclude.

    Declared low to high, but note that StrEnum comparison is alphabetical, not
    ordinal -- `RiskLevel.HIGH < RiskLevel.LOW` is True, which is nonsense. Use
    RISK_ORDER for ranking. That trap is why the order is written down as data
    instead of being left implied by declaration order.
    """

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


RISK_ORDER: dict[RiskLevel, int] = {
    RiskLevel.LOW: 0,
    RiskLevel.MEDIUM: 1,
    RiskLevel.HIGH: 2,
}


class Action(StrEnum):
    """What the executor does. Derived from risk, never stated by the model."""

    FULL_DEPLOY = "full_deploy"
    CANARY = "canary"
    HALT_AND_ESCALATE = "halt_and_escalate"


# The risk-to-action mapping from CLAUDE.md, in one readable place.
#
# Exhaustive over RiskLevel by construction: `action_for` raises on an unmapped
# member rather than defaulting, so adding a fourth risk level fails loudly here
# instead of silently deploying. A `.get(level, FULL_DEPLOY)` would have been
# shorter and is exactly the fail-open bug this project exists to talk about.
ACTION_FOR_RISK: dict[RiskLevel, Action] = {
    RiskLevel.LOW: Action.FULL_DEPLOY,
    RiskLevel.MEDIUM: Action.CANARY,
    RiskLevel.HIGH: Action.HALT_AND_ESCALATE,
}


def action_for(level: RiskLevel) -> Action:
    try:
        return ACTION_FOR_RISK[level]
    except KeyError as exc:  # pragma: no cover - unreachable while the enum is closed
        raise ValueError(f"no action mapped for risk level {level!r}") from exc


class VerdictSource(StrEnum):
    """Where a verdict came from. Load-bearing for measurement, not for routing.

    A fail-closed HIGH and a model-assessed HIGH produce identical behaviour --
    both halt and escalate -- but they mean completely different things when
    Phase 4 measures over-flagging. Without this field, a week of Bedrock
    throttling would read as a week of the gate correctly catching risky
    changes, which is the most flattering possible way to be wrong.
    """

    MODEL = "model"
    FAIL_CLOSED = "fail_closed"
    HUMAN_OVERRIDE = "human_override"


@dataclass(frozen=True, slots=True)
class Verdict:
    """A validated risk verdict, or a fail-closed stand-in for one.

    Frozen because a verdict is a record of a decision that was made. Mutating
    one after the fact would corrupt the audit trail, which CLAUDE.md treats as
    a demo asset rather than mere hygiene.
    """

    risk_level: RiskLevel
    reasoning: str
    source: VerdictSource
    confidence: float | None = None
    primary_concerns: tuple[str, ...] = ()

    # --- audit fields ----------------------------------------------------
    # None on a fail-closed verdict produced before any model was reached.
    model_id: str | None = None
    # Set when a prose field exceeded its bound and was shortened. Without this,
    # a truncated `reasoning` would be indistinguishable from one the model
    # simply wrote short, and the audit record would be quietly lying.
    truncated_fields: tuple[str, ...] = ()

    # Phase 5.5. Set when a deterministic floor raised this verdict above what
    # the model said (verdict/floor.py), carrying the level the model actually
    # returned and why the floor overrode it.
    #
    # Recorded rather than silently applied, for the same reason `source`
    # exists: without it, a `medium` the code insisted on and a `medium` the
    # model reasoned its way to are indistinguishable in the table -- and the
    # whole point of the floor is that the model kept saying `low`. Phase 4
    # would then measure the floor's arithmetic and call it the model's
    # judgement.
    floor_raised_from: RiskLevel | None = None
    floor_reason: str = ""

    def __post_init__(self) -> None:
        if self.source is VerdictSource.MODEL and self.confidence is None:
            raise ValueError("a model-sourced verdict must carry the confidence the model reported")
        if self.source is VerdictSource.FAIL_CLOSED and self.confidence is not None:
            raise ValueError(
                "a fail-closed verdict has no confidence; the model never supplied one. "
                "Recording 0.0 would fabricate a measurement Phase 4 would average in."
            )
        if not self.reasoning:
            raise ValueError("every verdict must record why it reached its conclusion")

    def raised_to(self, level: RiskLevel, reason: str) -> Verdict:
        """A copy at a higher risk level, recording what the model had said.

        Refuses to lower, because a floor that could lower a verdict is not a
        floor -- and because the one thing this must never do is give the model
        a route to a safer-looking answer than it argued for.
        """
        if RISK_ORDER[level] <= RISK_ORDER[self.risk_level]:
            raise ValueError(
                f"a floor may only raise a verdict; {self.risk_level} -> {level} is not an increase"
            )
        return replace(
            self,
            risk_level=level,
            floor_raised_from=self.risk_level,
            floor_reason=reason,
        )

    @property
    def was_raised_by_floor(self) -> bool:
        return self.floor_raised_from is not None

    @property
    def action(self) -> Action:
        """The action this verdict recommends, before mode is considered.

        Mode -- shadow, advisory, enforcing -- decides whether the action is
        actually taken. That stays in the handler, so this property is a pure
        function of risk and is safe to assert on in eval fixtures.
        """
        return action_for(self.risk_level)

    @property
    def is_blocking(self) -> bool:
        return self.action is Action.HALT_AND_ESCALATE

    @classmethod
    def fail_closed(cls, reason: str, *, model_id: str | None = None) -> Verdict:
        """The default branch. HIGH risk, no confidence, reason recorded.

        Reusing HIGH rather than inventing a fourth "unknown" level is
        deliberate: the executor keeps exactly three branches no matter how many
        ways the gate can fail, and "halt and escalate to a human" is already
        precisely what CLAUDE.md asks for when a signal or the model is missing.
        `source` is what preserves the distinction that matters.
        """
        return cls(
            risk_level=RiskLevel.HIGH,
            reasoning=reason,
            source=VerdictSource.FAIL_CLOSED,
            confidence=None,
            model_id=model_id,
        )

    def to_dict(self) -> dict[str, object]:
        """Audit-record form. Flat, JSON-safe, and stable across runs."""
        return {
            "risk_level": str(self.risk_level),
            "action": str(self.action),
            "source": str(self.source),
            "confidence": self.confidence,
            "reasoning": self.reasoning,
            "primary_concerns": list(self.primary_concerns),
            "model_id": self.model_id,
            "truncated_fields": list(self.truncated_fields),
            # Phase 5.5. None on the ordinary path. When set, `risk_level` above
            # is NOT what the model said -- it is what the evidence required, and
            # this is the only field that says so. Measuring the gate's
            # calibration without it would credit the model for a level that
            # fifteen lines of arithmetic insisted on.
            "floor_raised_from": (
                str(self.floor_raised_from) if self.floor_raised_from is not None else None
            ),
            "floor_reason": self.floor_reason,
        }
