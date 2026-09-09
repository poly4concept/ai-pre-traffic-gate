"""The timeout numbers still add up. Phase 5.5.

WHY THIS EXISTS

Four values nest inside each other, live in three different files, and were
derived together against one model's measured latency:

    Lambda timeout (gate_stub.tf)          60s
      signal collection                    ~3.6s   measured
      DEADLINE_SECONDS (bedrock.py)        35s
        READ_TIMEOUT_SECONDS               20s     per attempt
        BACKOFF_SECONDS                    between attempts
      audit write + escalation             ~0.5s   measured

Raising any ONE of them in isolation is a bug. Raise the read timeout and the
worst case punches through the deadline; raise the deadline and it punches
through the Lambda timeout; and a Lambda that times out mid-call reports a
Lambda timeout rather than the gate reporting a fail-closed verdict -- losing
the audit record and the escalation on exactly the runs that most need them.

This is the arithmetic from the header of bedrock.py, executable. It is not
checking that the numbers are OPTIMAL -- that is a judgement about latency
measurements. It checks they are CONSISTENT, which is the part that silently
stops being true when somebody changes a model.
"""

from __future__ import annotations

import re
from pathlib import Path

from verdict.bedrock import (
    BACKOFF_SECONDS,
    CONNECT_TIMEOUT_SECONDS,
    DEADLINE_SECONDS,
    MAX_ATTEMPTS,
    READ_TIMEOUT_SECONDS,
)

ROOT = Path(__file__).resolve().parents[1]

# Measured on real invocations, not estimated. See D-080.
COLLECTION_SECONDS = 3.6
WRITE_AND_ESCALATE_SECONDS = 0.5

# Slowest verdict model latency observed, us-east-1, Sonnet 4.5.
OBSERVED_MAX_MODEL_SECONDS = 7.4


def lambda_timeout() -> int:
    """The gate function's timeout, read from the Terraform that sets it."""
    tf = (ROOT / "infra" / "personal" / "gate_stub.tf").read_text(encoding="utf-8")
    # The gate is the only aws_lambda_function in this file.
    match = re.search(r"^\s*timeout\s*=\s*(\d+)", tf, re.MULTILINE)
    assert match, "could not find the gate Lambda's timeout in gate_stub.tf"
    return int(match.group(1))


def worst_case_model_seconds() -> float:
    """Longest the retry loop can spend, given the predictive deadline check.

    Mirrors the loop in `get_verdict`: before retrying it asks whether there is
    room for a WHOLE further attempt, not merely to begin one.
    """
    elapsed = 0.0
    for attempt in range(MAX_ATTEMPTS):
        elapsed += READ_TIMEOUT_SECONDS
        if attempt + 1 >= MAX_ATTEMPTS:
            break
        delay = BACKOFF_SECONDS[min(attempt, len(BACKOFF_SECONDS) - 1)]
        if elapsed + delay + READ_TIMEOUT_SECONDS >= DEADLINE_SECONDS:
            break
        elapsed += delay
    return elapsed


def test_the_read_timeout_clears_the_slowest_model_we_have_measured():
    """A timeout should mean "something is wrong", not "the model was slow".

    The old value of 8s was 1.4x Sonnet 4.5's median and tripped on ordinary
    variance -- 2 of 69 eval calls -- which is a retry burned on one deploy in
    thirty, and a fail-closed halt whenever the retry is slow too.
    """
    assert READ_TIMEOUT_SECONDS >= 2 * OBSERVED_MAX_MODEL_SECONDS, (
        f"read timeout {READ_TIMEOUT_SECONDS}s leaves no headroom over the "
        f"{OBSERVED_MAX_MODEL_SECONDS}s slowest observed call"
    )


def test_one_full_attempt_fits_inside_the_deadline():
    """Otherwise the deadline forbids even the first attempt from completing,
    and every slow call fails closed regardless of what the model would have
    said."""
    assert READ_TIMEOUT_SECONDS < DEADLINE_SECONDS


def test_the_retry_loop_cannot_outrun_its_deadline():
    """THE PROPERTY THE PREDICTIVE CHECK BUYS.

    `elapsed + delay >= deadline` only asks whether there is time to START
    another attempt, and the deadline cannot interrupt a call already in flight.
    With a 20s read timeout and a 35s deadline that let a retry begin at 20.5s
    and run to 40.5s -- overshooting by six seconds every time it was hit.
    """
    assert worst_case_model_seconds() <= DEADLINE_SECONDS, (
        f"the loop can spend {worst_case_model_seconds()}s against a {DEADLINE_SECONDS}s deadline"
    )


def test_the_whole_handler_fits_inside_the_lambda_timeout():
    """The one that actually protects the audit trail.

    A Lambda that times out mid-call never reaches `write_audit_record` or
    `escalate`. The verdict is lost, nobody is emailed, and CodePipeline reports
    a function timeout instead of a fail-closed halt -- on precisely the runs
    where the gate was struggling and the record matters most.
    """
    worst = COLLECTION_SECONDS + worst_case_model_seconds() + WRITE_AND_ESCALATE_SECONDS

    assert worst <= lambda_timeout(), (
        f"worst case is {worst:.1f}s against a {lambda_timeout()}s Lambda timeout. "
        "Raise the function timeout in gate_stub.tf, or lower DEADLINE_SECONDS."
    )


def test_there_is_real_headroom_not_just_arithmetic_that_barely_clears():
    """Sitting exactly on a limit is the same as being over it the first time
    collection is slower than the day it was measured."""
    worst = COLLECTION_SECONDS + worst_case_model_seconds() + WRITE_AND_ESCALATE_SECONDS

    assert worst <= 0.75 * lambda_timeout(), (
        f"worst case {worst:.1f}s uses more than 75% of the {lambda_timeout()}s "
        "Lambda timeout; there is no room for a slow day"
    )


def test_connect_timeout_is_short_because_a_refused_connection_is_immediate():
    """Nothing is gained by waiting to be told a TCP connection failed, and the
    wait comes out of the same budget as the model call."""
    assert CONNECT_TIMEOUT_SECONDS <= 5
    assert CONNECT_TIMEOUT_SECONDS < READ_TIMEOUT_SECONDS


def test_the_documented_budget_matches_the_constants():
    """The derivation in bedrock.py's header is the explanation people will
    read. If it drifts from the code it becomes actively misleading."""
    source = (ROOT / "services" / "decision_service" / "verdict" / "bedrock.py").read_text(
        encoding="utf-8"
    )

    assert f"DEADLINE_SECONDS = {DEADLINE_SECONDS}" in source
    assert f"READ_TIMEOUT_SECONDS = {READ_TIMEOUT_SECONDS}" in source
    assert f"{lambda_timeout()}-second Lambda timeout" in source, (
        "bedrock.py's budget comment names a different Lambda timeout than "
        "gate_stub.tf actually sets"
    )
