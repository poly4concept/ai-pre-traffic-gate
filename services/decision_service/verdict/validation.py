"""The contract. Phase 3.1.

`schema.py` is what we ask the model for. This is what we insist on. It is the
only thing standing between a language model's output and a decision about
production, so it is written to be read in full rather than trusted.

THE RULE THAT DECIDES REJECT VS REPAIR

Not every schema violation deserves the same response, and getting this wrong in
either direction is costly. Reject everything and a model writing 615 characters
of perfectly sound reasoning halts a deploy over punctuation. Repair everything
and a model that plainly misunderstood the task still gets to set the risk
level.

The line drawn here:

  LENGTH AND COUNT OVERRUNS ARE REPAIRED.
      Models cannot reliably count characters. "At most 600 characters" is a
      request they will approximately honour, and a 610-character answer is not
      evidence of confusion -- it is evidence that token generation does not
      come with a character counter. Truncate, record that we truncated, carry
      on. The bound exists to limit how far text travels into logs and Slack,
      and truncation achieves that.

  TYPE, ENUM AND SHAPE ERRORS ARE REJECTED.
      A string where a number belongs, a risk level that is not one of the three,
      an extra key, a missing key. None of these are things a model does while
      understanding the contract. They are evidence it did not, and the moment we
      have that evidence the `risk_level` in the same object stops deserving
      trust. Reject, and let the caller fail closed.

Both branches are safe, which is what makes the distinction affordable: a
rejection becomes a fail-closed HIGH verdict, and a repair only ever shortens
text nobody acts on.

WHAT REJECTION DOES NOT DO

It does not raise into the void. `parse_verdict` raising is the normal, expected
path for a misbehaving model, and every caller turns it into
`Verdict.fail_closed(...)`. The exception type exists so the audit record can say
which rule was broken.
"""

from __future__ import annotations

from typing import Any

from .schema import (
    MAX_CONCERN_CHARS,
    MAX_CONCERNS,
    MAX_REASONING_CHARS,
    VERDICT_FIELDS,
)
from .types import RiskLevel, Verdict, VerdictSource

# Appended to a shortened string so a reader of the audit record can see the cut
# without cross-checking `truncated_fields`.
TRUNCATION_MARKER = "..."


class VerdictValidationError(ValueError):
    """The model returned something outside the contract it was given.

    Carries `field` so the audit record and Phase 4 can aggregate by which rule
    was broken -- "the model returns a bad enum twice a week" is a prompt
    problem, while "the model omits primary_concerns" is a schema-wording
    problem, and an undifferentiated error count cannot tell them apart.
    """

    def __init__(self, message: str, *, field: str | None = None):
        super().__init__(message)
        self.field = field


def parse_verdict(raw: Any, *, model_id: str | None = None) -> Verdict:
    """Validate raw tool input and build a Verdict, or raise.

    `raw` is whatever came back in the Converse response's `toolUse.input` --
    already JSON-decoded by botocore, so this is a dict of Python objects rather
    than a string. No `json.loads` here on purpose: adding one would silently
    accept a model that double-encoded its answer, which is a contract failure
    worth seeing.
    """
    if not isinstance(raw, dict):
        raise VerdictValidationError(
            f"tool input is {type(raw).__name__}, not an object; nothing to validate"
        )

    _check_key_set(raw)

    truncated: list[str] = []

    risk_level = _parse_risk_level(raw["risk_level"])
    confidence = _parse_confidence(raw["confidence"])
    reasoning = _parse_reasoning(raw["reasoning"], truncated)
    concerns = _parse_concerns(raw["primary_concerns"], truncated)

    return Verdict(
        risk_level=risk_level,
        reasoning=reasoning,
        source=VerdictSource.MODEL,
        confidence=confidence,
        primary_concerns=concerns,
        model_id=model_id,
        truncated_fields=tuple(truncated),
    )


# --- shape ----------------------------------------------------------------


def _check_key_set(raw: dict[str, Any]) -> None:
    """Exactly the four fields, no more and no fewer.

    Checked before any field is read so the error names every problem at once.
    Validating field-by-field would report the first missing key and hide the
    other three, which turns one prompt fix into four round trips.
    """
    expected = set(VERDICT_FIELDS)
    present = set(raw)

    missing = sorted(expected - present)
    if missing:
        raise VerdictValidationError(
            f"tool input is missing required field(s): {', '.join(missing)}",
            field=missing[0],
        )

    extra = sorted(present - expected)
    if extra:
        raise VerdictValidationError(
            f"tool input has unexpected field(s): {', '.join(extra)}; "
            "the model answered a different question than it was asked",
            field=extra[0],
        )


# --- fields we act on: reject on anything unexpected ----------------------


def _parse_risk_level(value: Any) -> RiskLevel:
    if not isinstance(value, str):
        raise VerdictValidationError(
            f"risk_level is {type(value).__name__}, expected one of "
            f"{[str(level) for level in RiskLevel]}",
            field="risk_level",
        )

    # Case and surrounding whitespace are normalised rather than rejected. "LOW"
    # is unambiguous, and halting a deploy over capitalisation would be a
    # self-inflicted outage. Anything that is still not an exact member after
    # normalising is a real disagreement about the vocabulary, and that is not
    # repairable -- there is no safe guess between "medium" and "high".
    normalised = value.strip().lower()
    try:
        return RiskLevel(normalised)
    except ValueError as exc:
        raise VerdictValidationError(
            f"risk_level {value!r} is not one of {[str(level) for level in RiskLevel]}",
            field="risk_level",
        ) from exc


def _parse_confidence(value: Any) -> float:
    """Rejected on type error even though confidence never routes anything.

    The reason is not that the number matters -- it is recorded and never acted
    on. The reason is that a type error here is evidence the model did not
    follow the schema, and that evidence applies to the whole object, including
    the `risk_level` we very much do act on.
    """
    # bool before int: `isinstance(True, int)` is True in Python, so a model
    # returning `true` for a number field would otherwise validate as 1.0 --
    # inside the allowed range, and completely meaningless.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise VerdictValidationError(
            f"confidence is {type(value).__name__}, expected a number between 0 and 1",
            field="confidence",
        )

    confidence = float(value)
    if not 0.0 <= confidence <= 1.0:
        raise VerdictValidationError(
            f"confidence {confidence} is outside the stated range 0 to 1",
            field="confidence",
        )
    return confidence


# --- prose: repair length, reject shape -----------------------------------


def _parse_reasoning(value: Any, truncated: list[str]) -> str:
    if not isinstance(value, str):
        raise VerdictValidationError(
            f"reasoning is {type(value).__name__}, expected a string",
            field="reasoning",
        )

    reasoning = value.strip()
    if not reasoning:
        # Not a length problem -- an empty justification is a refusal to
        # justify, and an unexplained verdict is useless in an audit trail and
        # unusable on stage.
        raise VerdictValidationError(
            "reasoning is empty; a verdict with no stated basis is not auditable",
            field="reasoning",
        )

    if len(reasoning) > MAX_REASONING_CHARS:
        reasoning = _shorten(reasoning, MAX_REASONING_CHARS)
        truncated.append("reasoning")

    return reasoning


def _parse_concerns(value: Any, truncated: list[str]) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise VerdictValidationError(
            f"primary_concerns is {type(value).__name__}, expected an array",
            field="primary_concerns",
        )

    concerns: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str):
            raise VerdictValidationError(
                f"primary_concerns[{index}] is {type(item).__name__}, expected a string",
                field="primary_concerns",
            )
        text = item.strip()
        # Empty entries are dropped rather than rejected. A trailing "" is a
        # formatting artefact, not a claim about the deploy, and it carries no
        # information worth halting over.
        if not text:
            continue
        if len(text) > MAX_CONCERN_CHARS:
            text = _shorten(text, MAX_CONCERN_CHARS)
            truncated.append(f"primary_concerns[{index}]")
        concerns.append(text)

    if len(concerns) > MAX_CONCERNS:
        # Kept in order: the schema asks for most important first, so the tail
        # is what we can afford to lose.
        truncated.append("primary_concerns")
        concerns = concerns[:MAX_CONCERNS]

    return tuple(concerns)


def _shorten(text: str, limit: int) -> str:
    """Cut to `limit` characters INCLUDING the marker.

    Off-by-one matters here in a way it usually does not: the whole point of the
    bound is that downstream systems can size for it, so a "truncated" string
    that comes back three characters over the limit defeats the exercise.
    """
    if limit <= len(TRUNCATION_MARKER):  # pragma: no cover - bounds are far larger
        return text[:limit]
    return text[: limit - len(TRUNCATION_MARKER)].rstrip() + TRUNCATION_MARKER
