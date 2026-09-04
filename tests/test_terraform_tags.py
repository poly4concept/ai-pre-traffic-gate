"""Every AWS tag value in the stack is one AWS will actually accept. Phase 5.3.

WHY THIS EXISTS

`terraform validate` passed. `terraform plan` passed and printed
`1 to add, 3 to change, 0 to destroy`. The apply then failed on the first
resource it touched:

    ValidationException: The Tag Value provided is invalid,
    Value: Human overrides of gate verdicts, scoped to one pipeline execution

The comma. AWS tag values allow letters, digits, whitespace and
`_ . : / = + - @`, and nothing else.

The reason nothing local caught it is the interesting part: the constraint
belongs to the SERVICE, not to the Terraform provider's schema. `validate`
checks syntax and schema; `plan` diffs desired state against real state and
sends nothing to DynamoDB's CreateTable validator. There is no local step that
could have known, so the first thing that can tell you is an apply that has
already started creating resources.

Hence a test. Fifteen lines that turn an apply-time failure into a test-time
one, for a class of error where the feedback loop is otherwise "type yes, wait,
read a 400".
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

INFRA = Path(__file__).resolve().parents[1] / "infra" / "personal"

# AWS's documented tag value character set: unicode letters, whitespace, digits,
# and _ . : / = + - @
TAG_VALUE_OK = re.compile(r"^[\w\s.:/=+\-@]*$", re.UNICODE)

# `Key = "value"` inside a tags block.
TAG_LINE = re.compile(r'^\s*([A-Za-z][\w-]*)\s*=\s*"([^"]*)"\s*$')

# `${...}` is Terraform syntax, not characters AWS will ever see. Its `$ { }`
# would fail the check on a value that resolves perfectly well, so each
# interpolation collapses to a placeholder and the LITERAL text around it is
# what gets validated.
#
# This is the honest limit of the guard, worth stating rather than papering
# over: it cannot see inside a variable. A comma in `var.project_name` would
# still reach AWS. What it does catch is the case that actually happened -- a
# comma somebody typed straight into the tag.
INTERPOLATION = re.compile(r"\$\{[^}]*\}")


def literal_part(value: str) -> str:
    return INTERPOLATION.sub("X", value)


def tag_values() -> list[tuple[str, str, str]]:
    """(file, key, value) for every tag assignment in the stack."""
    found: list[tuple[str, str, str]] = []

    for path in sorted(INFRA.glob("*.tf")):
        in_tags = False
        depth = 0
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not in_tags:
                if re.match(r"^tags\s*=\s*\{", stripped) or re.match(
                    r"^default_tags\s*\{", stripped
                ):
                    in_tags, depth = True, stripped.count("{") - stripped.count("}")
                continue

            depth += stripped.count("{") - stripped.count("}")
            if depth <= 0:
                in_tags = False
                continue
            match = TAG_LINE.match(line)
            if match:
                found.append((path.name, match.group(1), match.group(2)))
    return found


def test_the_scanner_actually_finds_tags():
    """A scanner that silently matches nothing would pass forever.

    The real risk with a regex-based guard is not a false failure, it is a
    false pass -- so the first assertion is that it sees something at all.
    """
    values = tag_values()

    assert len(values) >= 5, f"expected to find tags across the stack, got {values}"
    assert any(key == "Purpose" for _, key, _ in values)


def test_every_tag_value_is_one_aws_will_accept():
    """THE APPLY-TIME FAILURE THIS TURNS INTO A TEST-TIME ONE.

    A comma in a tag value passes validate, passes plan, and fails CreateTable
    after the apply has begun.
    """
    bad = [
        (name, key, value)
        for name, key, value in tag_values()
        if not TAG_VALUE_OK.match(literal_part(value))
    ]

    assert not bad, (
        "AWS rejects these tag values (allowed: letters, digits, whitespace, "
        "and _ . : / = + - @):\n" + "\n".join(f"  {n}: {k} = {v!r}" for n, k, v in bad)
    )


@pytest.mark.parametrize(
    "value",
    [
        "Human overrides of gate verdicts, scoped to one pipeline execution",  # the real one
        "a,b",
        "why(not)",
        "100% sure",
        "a;b",
    ],
)
def test_the_check_rejects_what_aws_rejects(value):
    """Guards the guard. A pattern that accepts everything is not a check."""
    assert not TAG_VALUE_OK.match(literal_part(value))


@pytest.mark.parametrize(
    "value",
    [
        "Human overrides of gate verdicts",
        "phase 2.5 fault injection control",
        "ai-pre-traffic-gate-verdicts",
        "${var.project_name}-verdicts",  # interpolation is Terraform syntax, not a tag character
        "true",
        "role/service:thing=1+2@host",
    ],
)
def test_the_check_accepts_what_aws_accepts(value):
    """And the other direction, so it cannot be tightened into uselessness."""
    assert TAG_VALUE_OK.match(literal_part(value))
