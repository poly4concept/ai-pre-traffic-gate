"""A deterministic stand-in for the model. Phase 4a.

WHAT THIS IS AND IS NOT

It is a crude rule-based scorer: count some attributes, add up weights, map the
total to a risk level. It exists for two reasons.

  1. It makes the report runnable today, so the format can be reviewed before
     any real inference is possible.
  2. It is a BASELINE, and this is the more interesting reason.

WHY A BASELINE MATTERS MORE THAN IT SOUNDS

When the real model finally runs and scores, say, 80%, the immediate question is
"is that good?" -- and there is no way to answer it without something to compare
against. If a fifteen-line attribute-counter also scores 80%, then whatever the
model is contributing, it is not accuracy on this set, and paying per token for
it needs a different justification.

So this is the thing the model has to beat. Cheap, instant, free, and completely
incapable of judgement -- it cannot read a commit message, so it cannot know
that `revert_of_a_bad_deploy` is a revert, and it cannot notice a prompt
injection. Those are exactly the scenarios where a real model should pull ahead,
and the eval is built to show whether it does.

Deliberately NOT tuned to pass the eval. Tuning it would make it a second
labelling of the fixtures rather than an independent point of comparison, and
its failures are informative: they are the scenarios that need judgement rather
than arithmetic.
"""

from __future__ import annotations

from typing import Any

from verdict import ModelCall, RiskLevel, Verdict, VerdictOutcome, VerdictSource, apply_floor

# Paths whose contents tend to matter more when they break.
SENSITIVE_MARKERS = ("payments/", "auth/", "billing/", "settlement")

# Paths with no production surface at all.
#
# `.txt` is deliberately NOT here. It was, and it matched `requirements.txt` --
# a dependency manifest, which is about as production-affecting as a file gets.
# The baseline scored every CVE scenario `low` as a result, because the -2 for
# "inert paths" cancelled the +3 for a critical finding.
#
# Worth keeping the note: the baseline is meant to be crude, not broken. A
# suffix match that quietly swallows the most important file in a Python repo
# is the second kind, and it would have made the security comparisons
# meaningless while still producing a confident-looking number.
INERT_SUFFIXES = (".md", ".rst")
INERT_PREFIXES = ("tests/", "docs/")


def _is_inert(paths: tuple[str, ...]) -> bool:
    if not paths:
        return False
    return all(path.endswith(INERT_SUFFIXES) or path.startswith(INERT_PREFIXES) for path in paths)


class AttributeCountingClient:
    """Scores a bundle by counting attributes. The baseline to beat."""

    MODEL_ID = "baseline:attribute-counting"

    def get_verdict(self, bundle: Any) -> VerdictOutcome:
        # The one thing it gets right for the right reason: no change context
        # means nothing to judge. Encoded here too so the baseline is compared
        # on the same fail-closed terms as the real client.
        if not bundle.has_required_signals:
            return VerdictOutcome(
                verdict=Verdict.fail_closed(
                    f"required signals missing: {bundle.completeness_summary}"
                ),
                call=ModelCall(
                    model_id=self.MODEL_ID,
                    prompt_version="baseline",
                    attempts=0,
                    succeeded=False,
                    failure_kind="required_signals_missing",
                ),
                raw_model_output=None,
            )

        score = 0
        notes: list[str] = []
        change = bundle.change.data
        health = bundle.health.data if bundle.health.is_usable else None
        security = bundle.security.data if bundle.security.is_usable else None

        if change is not None:
            if _is_inert(change.paths):
                score -= 2
                notes.append("no production files touched")
            if change.total_lines_changed > 500:
                score += 2
                notes.append(f"{change.total_lines_changed} lines changed")
            elif change.total_lines_changed > 100:
                score += 1
            if any(m in p for p in change.paths for m in SENSITIVE_MARKERS):
                score += 2
                notes.append("touches a sensitive path")
            if change.is_off_hours:
                score += 1
                notes.append("deployed outside business hours")
            if change.deploys_last_24h is not None and change.deploys_last_24h >= 5:
                score += 1
                notes.append("repeated deploys in the last day")

        if security is not None:
            if security.critical_count:
                score += 3
                notes.append(f"{security.critical_count} critical finding(s)")
            elif security.high_count:
                score += 2
            if security.unknown_count:
                score += 1
                notes.append("findings of unknown severity")
        else:
            score += 1
            notes.append("no security signal available")

        if health is not None:
            if health.has_active_alarm:
                score += 3
                notes.append("target has an active alarm")
            if not health.has_health_evidence:
                score += 1
                notes.append("no usable health evidence")
        else:
            score += 1
            notes.append("no health signal available")

        level = RiskLevel.LOW
        if score >= 5:
            level = RiskLevel.HIGH
        elif score >= 2:
            level = RiskLevel.MEDIUM

        reasoning = f"Attribute score {score}. " + (
            "; ".join(notes) if notes else "nothing notable."
        )

        # Phase 5.5. The floor is part of the GATE, not part of the model, so
        # the baseline gets it too -- otherwise the comparison is
        # gate-with-a-floor against arithmetic-without-one, which flatters the
        # model by exactly the amount the floor is worth.
        #
        # It also raises the bar honestly: the floor IS arithmetic, so a
        # baseline denied it would be an artificially weak opponent.
        floored = apply_floor(
            Verdict(
                risk_level=level,
                reasoning=reasoning[:600],
                source=VerdictSource.MODEL,
                # A fixed value, and honestly meaningless -- which is the
                # point. A number in this field is not evidence of
                # calibration, whether it comes from arithmetic or from a
                # language model.
                confidence=0.5,
                primary_concerns=tuple(notes[:5]),
                model_id=self.MODEL_ID,
            ),
            bundle,
        )

        return VerdictOutcome(
            # Returned as-is, so `floor_raised_from` survives into the audit
            # record and the eval. Rebuilding it field by field would drop
            # exactly the field that says the arithmetic, not the scoring,
            # produced this level.
            verdict=floored,
            call=ModelCall(
                model_id=self.MODEL_ID,
                prompt_version="baseline",
                attempts=1,
                succeeded=True,
                input_tokens=0,
                output_tokens=0,
                latency_ms=0,
            ),
            raw_model_output={"score": score, "risk_level": str(level)},
        )
