"""The eval harness. Phase 4.

CLAUDE.md: "I need to measure over-flagging and detect prompt drift as the
prompt evolves. This gates everything after it."

    labels.py    expected verdicts, written before any model could be called
    harness.py   run the scenarios, score the result
    report.py    format it for a terminal
    stub.py      an attribute-counting baseline the model has to beat
    run.py       CLI

WHY THIS LIVES AT THE REPO ROOT

`archive_file` zips the whole of `services/decision_service`, so anything under
it ships to Lambda. A benchmark has no business in a production artifact. The
fixtures themselves stay in `signals/scenarios.py` because the demo and the test
suite both use them, but the labels and the scoring live out here.

WHY THIS FILE RE-EXPORTS NOTHING

The obvious `from .harness import run_eval` here would run at package-import
time, and `harness` imports `signals` -- which is not an installed package. It
lives in the Lambda's zip root and is put on `sys.path` by `run.py` and by the
test conftest. Re-exporting would therefore import `signals` BEFORE either of
them had a chance to make it importable, and `python -m evals.run` would fail
with ModuleNotFoundError before its first line ran.

So callers import the submodule they want. Slightly more typing, and it keeps
the package importable no matter what is on the path.

WHERE TO START READING

`labels.py`, and specifically the note on why a set of acceptable answers beats
one right answer. The scoring is only as good as the labels, and the labels are
the part with opinions in them.
"""
