"""Artifacts are generated, and say where they came from. Phase 6.3.

The talk claims specific numbers. Each one has to be traceable to a run, six
weeks after the run happened, on stage, when somebody asks.

So the tests are less about formatting than about two properties:

  1. Every artifact names its source.
  2. The numbers in it come from the result files, not from a literal somebody
     typed -- because a typed number cannot go stale visibly, it just goes
     wrong.

The AWS-backed parts are exercised offline; there is no network here.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def load(name: str):
    spec = importlib.util.spec_from_file_location(f"_test_{name}", SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


ca = load("capture_artifacts")


def result(**over):
    base = {
        "model_id": "m",
        "repeats": 3,
        "under_flagging": [3, 11],
        "over_flagging": [1, 11],
        "passes": [18, 22],
        "stability": [23, 23],
        "tokens": [187221, 15320],
        "fail_closed_attempts": 3,
        "pair_violations": [],
        "scenarios": [
            {
                "scenario": "critical_cve_no_patch",
                "modal": "low",
                "passed": False,
                "acceptable": ["high", "medium"],
                "kind": "risky",
                "levels": ["low"],
                "stable": True,
                "direction": "under",
                "concern_coverage": 0.0,
                "reasoning": "r",
            }
        ],
    }
    return {**base, **over}


def seed(tmp_path, monkeypatch, files):
    monkeypatch.setattr(ca, "RESULTS", tmp_path)
    for name, payload in files.items():
        (tmp_path / name).write_text(json.dumps(payload), encoding="utf-8")


# --- Rates -----------------------------------------------------------------


def test_a_rate_with_no_denominator_is_not_zero_percent():
    """Same rule the whole project runs on. `0/0` is undefined, and printing
    0.0% on a slide would assert something nobody measured."""
    assert ca._rate([0, 0]) == "n/a"
    assert ca._rate([3, 11]).startswith("27.3%")


def test_a_percentage_is_rounded_for_a_human():
    """CloudWatch returns 50.656167979002625 and the audit record keeps it. On a
    slide that is noise pretending to be precision."""
    assert ca._pct("50.656167979002625") == "50.7%"
    assert ca._pct(None) == "unknown"
    assert ca._pct("not a number") == "unknown"


# --- The tables ------------------------------------------------------------


def test_the_comparison_names_every_source_file(tmp_path, monkeypatch):
    """PROVENANCE IS THE POINT. A table with no source is a claim, not evidence."""
    seed(
        tmp_path,
        monkeypatch,
        {
            "2026-09-08-haiku-asymmetric.json": result(),
            "sonnet-4-5.json": result(),
            "nova-pro.json": result(under_flagging=[2, 11]),
        },
    )

    out = ca.model_comparison()

    for name in ("2026-09-08-haiku-asymmetric.json", "sonnet-4-5.json", "nova-pro.json"):
        assert name in out


def test_the_comparison_computes_rates_rather_than_quoting_them(tmp_path, monkeypatch):
    """Change the input, the output must change. A hardcoded 27.3% would pass a
    weaker test and be wrong the next time anything is re-run."""
    seed(
        tmp_path,
        monkeypatch,
        {
            "2026-09-08-haiku-asymmetric.json": result(under_flagging=[5, 10]),
            "sonnet-4-5.json": result(),
        },
    )

    out = ca.model_comparison()

    assert "50.0% (5/10)" in out
    assert "27.3% (3/11)" in out


def test_the_comparison_is_skipped_rather_than_faked_when_data_is_missing(tmp_path, monkeypatch):
    """One result file is not a comparison. Returning None means the index says
    it was not generated, instead of a table with a blank column."""
    seed(tmp_path, monkeypatch, {"sonnet-4-5.json": result()})

    assert ca.model_comparison() is None


def test_only_disagreements_are_listed(tmp_path, monkeypatch):
    """A row where all three models agree carries no information and pushes the
    interesting rows off the slide."""
    agreed = result(
        scenarios=[
            {**result()["scenarios"][0], "scenario": "everyone_agrees", "modal": "low"},
        ]
    )
    seed(
        tmp_path,
        monkeypatch,
        {"2026-09-08-haiku-asymmetric.json": agreed, "sonnet-4-5.json": agreed},
    )

    out = ca.model_comparison()

    assert "everyone_agrees" not in out


def test_the_floor_artifact_reports_what_moved_and_what_did_not(tmp_path, monkeypatch):
    before = result(under_flagging=[3, 11], passes=[18, 22])
    after = result(
        under_flagging=[0, 11],
        passes=[21, 22],
        scenarios=[{**result()["scenarios"][0], "modal": "medium", "passed": True}],
    )
    seed(tmp_path, monkeypatch, {"sonnet-4-5.json": before, "sonnet-4-5-with-floor.json": after})

    out = ca.floor_effect()

    assert "27.3% (3/11)" in out
    assert "0.0% (0/11)" in out
    assert "critical_cve_no_patch" in out
    assert "Exactly 1 scenarios changed" in out


def test_the_floor_artifact_lists_what_it_cannot_fix(tmp_path, monkeypatch):
    """The honest half. `revert_of_a_bad_deploy` fails on every model and no
    countable rule reaches it -- leaving that out would be the artifact
    flattering the system."""
    after = result(
        scenarios=[
            {
                **result()["scenarios"][0],
                "scenario": "revert_of_a_bad_deploy",
                "modal": "high",
                "passed": False,
                "acceptable": ["low", "medium"],
            }
        ]
    )
    seed(tmp_path, monkeypatch, {"sonnet-4-5.json": result(), "sonnet-4-5-with-floor.json": after})

    out = ca.floor_effect()

    assert "What the floor cannot fix" in out
    assert "revert_of_a_bad_deploy" in out


# --- Verdict rendering -----------------------------------------------------


def test_alarms_are_counted_only_when_actually_in_alarm():
    health = {
        "alarms": {
            "L": [
                {"M": {"state": {"S": "ALARM"}}},
                {"M": {"state": {"S": "OK"}}},
                {"M": {"state": {"S": "INSUFFICIENT_DATA"}}},
            ]
        }
    }

    assert ca._alarms_firing(health) == 1


def test_an_absent_alarm_list_counts_zero_rather_than_raising():
    assert ca._alarms_firing({}) == 0


# --- The metrics page ------------------------------------------------------


def test_the_test_count_is_summed_rather_than_scraped_from_a_summary_line():
    """First attempt matched a "N tests collected" line that this pytest version
    does not print with `-q`, so the page read `tests: unknown`. Summing the
    per-file counts does not depend on summary wording."""
    count = ca._test_count()

    assert count != "unknown"
    assert "across" in count and "files" in count


def test_counting_decisions_and_failures_reads_the_real_files():
    """These are talk numbers -- "82 decisions, 28 failures" goes on a slide --
    so they are counted, never typed."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]

    assert ca._count_entries(root / "DECISIONS.md", "## D-") >= 80
    assert ca._count_entries(root / "FAILURES.md", "## F-") >= 25
    assert ca._count_entries(root / "does-not-exist.md", "## D-") == 0


def test_the_metrics_page_omits_live_figures_offline_rather_than_guessing(tmp_path, monkeypatch):
    """`--offline` has to produce a page that is honest about what is missing.
    A metrics page with a plausible-looking invented number is worse than one
    with a gap."""
    seed(tmp_path, monkeypatch, {"sonnet-4-5.json": result()})

    page = ca.metrics("us-east-1", offline=True)

    assert "Not generated" in page
    assert "--offline" in page
    assert "Cost Explorer" not in page


def test_the_metrics_page_states_its_sources(tmp_path, monkeypatch):
    seed(tmp_path, monkeypatch, {"sonnet-4-5.json": result()})

    page = ca.metrics("us-east-1", offline=True)

    assert "_Source:" in page
    assert "evals/results" in page


def test_verdict_stats_ignore_manual_invokes(monkeypatch):
    """Same distinction gate_history.py draws: a hand invoke has no pipeline
    execution, fails closed to HIGH, and would inflate every figure on the
    page."""
    captured = {}

    class FakeDdb:
        def scan(self, **kw):
            captured["table"] = kw["TableName"]
            return {
                "Items": [
                    {
                        "pipeline_execution_id": {"S": "exec-1"},
                        "verdict": {"M": {"risk_level": {"S": "high"}}},
                        "action_taken": {"S": "halt_pipeline"},
                        "model_call": {"M": {"latency_ms": {"N": "3000"}}},
                    },
                    {  # a manual invoke -- no execution id
                        "verdict": {"M": {"risk_level": {"S": "high"}}},
                        "action_taken": {"S": "none"},
                        "model_call": {"M": {"latency_ms": {"N": "9999"}}},
                    },
                ]
            }

    monkeypatch.setattr(ca, "_attr", ca._attr)
    import boto3

    monkeypatch.setattr(boto3, "client", lambda *a, **k: FakeDdb())

    stats = ca._verdict_stats("us-east-1")

    assert stats["count"] == 1
    assert stats["halted"] == 1
    assert stats["median_latency"] == 3000, "the manual invoke must not skew latency"
