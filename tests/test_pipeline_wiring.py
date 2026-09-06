"""The pipeline puts values where the code reads them. Phase 5.4.

WHY THIS FILE EXISTS, AND IT IS THE MOST USEFUL THING IN PHASE 5.4

Phase 5.1 built the executor's verdict lookup. Phase 5.3 built the human
override path. Both are keyed by pipeline execution ID. Both were completely
non-functional from the day they shipped, and 705 tests passed.

The code read `job.data.pipelineContext.pipelineExecutionId`. That key does not
exist in a CodePipeline Lambda-invoke event -- it belongs to the custom-action
job structure returned by `PollForJobs`. And the test fixture had been written
to match the code's assumption, so the fixture and the bug agreed with each
other and the suite was green.

    a test built from an event shape you invented
    validates your assumption, not the integration

No unit test can know the real shape of a third-party event. What a test CAN do
is assert that the two halves of a contract refer to the same thing: that the
pipeline definition writes the field the handler reads. That is a contract both
sides of which live in this repository, so it is checkable -- and getting it
wrong is exactly what happened.

Same shape as `test_executor.py`'s AppSpec-drift assertion and
`test_terraform_tags.py`: a cheap test standing where two components have to
agree and nothing else forces them to.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PIPELINE_TF = (ROOT / "infra" / "personal" / "pipeline.tf").read_text(encoding="utf-8")

# The field name both sides must agree on. If this is renamed in one place and
# not the other, everything keyed by it silently stops working -- which is the
# failure this file exists to prevent recurring.
FIELD = "pipeline_execution_id"

# CodePipeline's built-in variable. Available to every action without a
# namespace declaration; the syntax is `#{...}` and getting it subtly wrong
# (`${...}`, or a mistyped name) yields a literal string rather than an error.
VARIABLE = "#{codepipeline.PipelineExecutionId}"


def action_blocks() -> dict[str, str]:
    """Rough per-action slices of the pipeline definition, keyed by stage name."""
    blocks: dict[str, str] = {}
    for match in re.finditer(r'stage\s*\{\s*name\s*=\s*"(\w+)"', PIPELINE_TF):
        start = match.start()
        nxt = PIPELINE_TF.find("stage {", match.end())
        blocks[match.group(1)] = PIPELINE_TF[start : nxt if nxt > 0 else len(PIPELINE_TF)]
    return blocks


def test_the_stages_are_the_ones_we_think():
    """Guards the parser. A slicer that finds nothing would pass every test
    below it forever."""
    stages = action_blocks()

    assert {"Source", "Build", "Gate", "Deploy"} <= set(stages)


@pytest.mark.parametrize("stage", ["Gate", "Deploy"])
def test_both_lambda_actions_receive_the_execution_id(stage):
    """THE ASSERTION THAT WOULD HAVE CAUGHT F-023.

    The gate needs it to file the verdict and to look up an override; the
    executor needs it to find the verdict the gate filed. Neither can obtain it
    from the invoke event on its own.
    """
    block = action_blocks()[stage]

    assert FIELD in block, f"{stage} action does not pass {FIELD} in UserParameters"
    assert VARIABLE in block, f"{stage} action does not interpolate {VARIABLE}"


def test_the_variable_uses_the_hash_brace_syntax_codepipeline_actually_expands():
    """`${...}` is Terraform's syntax and `#{...}` is CodePipeline's.

    Confusing them does not error. Terraform would try to resolve `${...}` at
    plan time and fail loudly, which is fine -- but a mistyped `#{...}` name is
    passed through as a LITERAL STRING to the Lambda, so the handler receives
    the characters `#{codepipeline.PipelineExecutionId}` and treats them as an
    execution ID. Every lookup then misses, silently, exactly as in F-023.
    """
    assert "${codepipeline.PipelineExecutionId}" not in PIPELINE_TF
    assert VARIABLE in PIPELINE_TF


def test_the_handlers_read_the_field_the_pipeline_writes():
    """The other half of the contract.

    Asserted against the source text rather than by calling the functions,
    because the point is that the NAME matches -- a behavioural test would pass
    just as happily if both sides agreed on the wrong name, which is precisely
    the state this project was in.
    """
    gate = (ROOT / "services" / "decision_service" / "handler.py").read_text(encoding="utf-8")
    executor = (ROOT / "services" / "executor" / "handler.py").read_text(encoding="utf-8")

    for name, source in (("gate", gate), ("executor", executor)):
        assert "UserParameters" in source, f"{name} does not read UserParameters"
        assert FIELD in source, f"{name} does not read {FIELD}"


def test_userparameters_stays_within_codepipelines_limit():
    """CodePipeline caps UserParameters at 1000 characters and rejects the whole
    pipeline definition when it is exceeded -- an error a long way from its
    cause.

    Adding the execution ID in 5.4 cost ~62 characters of a budget the change
    context was already close to filling, which is why MAX_B64_LENGTH dropped
    from 860 to 780. This asserts the arithmetic still works.
    """
    build = (ROOT / "scripts" / "build_change_context.py").read_text(encoding="utf-8")
    budget = int(re.search(r"MAX_B64_LENGTH = (\d+)", build).group(1))

    # {"trusted_commit_sha":"<40>","change_context_b64":"<N>",
    #  "pipeline_execution_id":"<36 uuid>"}
    fixed = len('{"trusted_commit_sha":"","change_context_b64":"","pipeline_execution_id":""}')
    worst_case = fixed + 40 + 36 + budget

    assert worst_case <= 1000, (
        f"worst-case UserParameters is {worst_case} chars, over CodePipeline's "
        f"1000 limit. Lower MAX_B64_LENGTH (currently {budget})."
    )
