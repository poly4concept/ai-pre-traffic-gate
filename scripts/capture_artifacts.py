#!/usr/bin/env python3
"""Generate the talk's artifacts from the data they came from. Phase 6.3.

    python scripts/capture_artifacts.py            # everything it can reach
    python scripts/capture_artifacts.py --offline  # skip anything needing AWS

WHY GENERATED RATHER THAN WRITTEN

Every number in this talk is a claim: 27.3% under-flagging, 0.0% after the
floor, $0.012 a verdict, 5.76s median latency. Each one is currently true and
each one will stop being true the moment something is re-run.

A number typed into a slide is a number nobody can re-derive. Six weeks from
now, on stage, "where did 27.3% come from?" has to have an answer better than
"I remember measuring it". So these are produced from `evals/results/*.json` and
from the live verdict table, and regenerating them is one command.

The uncomfortable version of the same point: this project's failures log has
three entries where a confident claim outlived the thing that made it true
(F-016, F-022, F-023). A slide deck is exactly that hazard with an audience.

WHAT IT PRODUCES, in demo/artifacts/

    README.md              index, with when each artifact was generated
    model-comparison.md    the three-model table -- the central finding
    floor-effect.md        before and after the deterministic floor
    verdict-<id>.md        one real verdict, rendered readably
    gate-history.md        what the gate did to real deploys

PROVENANCE IS PART OF THE ARTIFACT. Every file names the source file or table it
was built from, so a number on a slide can be traced back to a run.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "evals" / "results"
ARTIFACTS = ROOT / "demo" / "artifacts"
PROJECT = os.environ.get("PROJECT_NAME", "ai-pre-traffic-gate")

_USE_COLOUR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def _c(text: str, colour: str) -> str:
    if not _USE_COLOUR:
        return text
    return f"\033[{ {'green': '32', 'red': '31', 'dim': '90', 'bold': '1'}[colour] }m{text}\033[0m"


def _rate(pair: list[int]) -> str:
    n, d = pair
    return f"{100 * n / d:.1f}% ({n}/{d})" if d else "n/a"


def _load(name: str) -> dict | None:
    path = RESULTS / name
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    data["_source"] = name
    return data


def _stamp() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")


# --- the central finding ---------------------------------------------------


def model_comparison() -> str | None:
    """Three models, one fixture set. The table D-080 and D-081 rest on."""
    runs = [
        ("Haiku 4.5", _load("2026-09-08-haiku-asymmetric.json")),
        ("Sonnet 4.5", _load("sonnet-4-5.json")),
        ("Nova Pro", _load("nova-pro.json")),
    ]
    runs = [(label, r) for label, r in runs if r]
    if len(runs) < 2:
        return None

    lines = [
        "# Model comparison",
        "",
        f"_Generated {_stamp()} by `scripts/capture_artifacts.py`._",
        "",
        "Same 23 scenarios, three repeats each, temperature 0, identical prompt.",
        "**Before the deterministic floor** -- see `floor-effect.md` for after.",
        "",
        "| | " + " | ".join(label for label, _ in runs) + " |",
        "| --- |" + " --- |" * len(runs),
    ]

    rows = [
        ("under-flagging", lambda r: _rate(r["under_flagging"])),
        ("over-flagging", lambda r: _rate(r["over_flagging"])),
        ("acceptable verdict", lambda r: _rate(r["passes"])),
        ("stable across repeats", lambda r: _rate(list(r["stability"]))),
        ("input tokens (69 calls)", lambda r: f"{r['tokens'][0]:,}"),
    ]
    for name, fn in rows:
        lines.append(f"| {name} | " + " | ".join(fn(r) for _, r in runs) + " |")

    # Which scenarios each model failed. The aggregate is where you look; this
    # is what you find (D-081).
    lines += ["", "## Where they disagree", "", "Scenarios not all three agreed on:", ""]
    lines.append("| scenario | " + " | ".join(label for label, _ in runs) + " |")
    lines.append("| --- |" + " --- |" * len(runs))

    by_model = [(label, {s["scenario"]: s for s in r["scenarios"]}) for label, r in runs]
    names = sorted(by_model[0][1])
    for scenario in names:
        cells = []
        for _, table in by_model:
            s = table.get(scenario)
            cells.append("-" if s is None else f"{s['modal']}{'' if s['passed'] else ' **!**'}")
        if len(set(cells)) > 1:
            lines.append(f"| `{scenario}` | " + " | ".join(cells) + " |")

    lines += [
        "",
        "`!` marks a failure against the label. ",
        "",
        "**The finding:** Nova Pro has the best headline under-flagging rate and wins it "
        "entirely on the three security scenarios -- the ones the deterministic floor was "
        "about to remove from the model's job -- while failing "
        "`prompt_injection_in_commit_message`, which no code can fix. "
        "A headline rate can be right for the wrong reasons (D-081).",
        "",
        "**Source:** " + ", ".join(f"`evals/results/{r['_source']}`" for _, r in runs),
    ]
    return "\n".join(lines) + "\n"


def floor_effect() -> str | None:
    """The floor's measured effect, which is the Phase 5.5 result."""
    before = _load("sonnet-4-5.json")
    after = _load("sonnet-4-5-with-floor.json")
    if not (before and after):
        return None

    lines = [
        "# The deterministic security floor",
        "",
        f"_Generated {_stamp()} by `scripts/capture_artifacts.py`._",
        "",
        "Two rules, both countable, both security:",
        "",
        "```",
        "any critical finding        -> at least MEDIUM",
        "any unknown-severity find   -> at least MEDIUM",
        "```",
        "",
        "The model may go higher. It may never go lower.",
        "",
        "| | before | with floor |",
        "| --- | --- | --- |",
        f"| **under-flagging** | {_rate(before['under_flagging'])} "
        f"| **{_rate(after['under_flagging'])}** |",
        f"| over-flagging | {_rate(before['over_flagging'])} | {_rate(after['over_flagging'])} |",
        f"| acceptable | {_rate(before['passes'])} | {_rate(after['passes'])} |",
        "",
    ]

    a = {s["scenario"]: s for s in before["scenarios"]}
    b = {s["scenario"]: s for s in after["scenarios"]}
    moved = [
        k
        for k in sorted(a)
        if k in b and (a[k]["modal"], a[k]["passed"]) != (b[k]["modal"], b[k]["passed"])
    ]
    lines += ["## What moved", "", "| scenario | before | after |", "| --- | --- | --- |"]
    for k in moved:
        lines.append(
            f"| `{k}` | {a[k]['modal']}{'' if a[k]['passed'] else ' **!**'} "
            f"| {b[k]['modal']}{'' if b[k]['passed'] else ' **!**'} |"
        )

    still = [k for k in sorted(b) if not b[k]["passed"]]
    lines += [
        "",
        f"Exactly {len(moved)} scenarios changed, and the over-flagging rate did not move "
        "by one. The gain came from precision, not from becoming indiscriminately cautious.",
        "",
        "## What the floor cannot fix",
        "",
    ]
    for k in still:
        lines.append(f"- `{k}` -- got `{b[k]['modal']}`, wanted one of {b[k]['acceptable']}")
    lines += [
        "",
        "No countable rule reaches these. `revert_of_a_bad_deploy` needs reading the commit "
        "message and understanding that the change IS the fix -- every signal says halt and "
        "halting is wrong. Haiku, Sonnet and Nova all miss it.",
        "",
        f"**Source:** `evals/results/{before['_source']}`, `evals/results/{after['_source']}`",
    ]
    return "\n".join(lines) + "\n"


# --- from the live account -------------------------------------------------


def _attr(item: dict, *path: str) -> str:
    node = item
    for key in path:
        if not isinstance(node, dict):
            return ""
        node = node.get(key)
        if node is None:
            return ""
        if "M" in node and key != path[-1]:
            node = node["M"]
    if not isinstance(node, dict):
        return ""
    for kind in ("S", "N"):
        if kind in node:
            return str(node[kind])
    return ""


def _pct(raw: str | None) -> str:
    """A percentage a human reads, not a float a machine emitted.

    CloudWatch returns 50.656167979002625 and the audit record faithfully keeps
    it. On a slide that is noise pretending to be precision.
    """
    try:
        return f"{float(raw):.1f}%"
    except (TypeError, ValueError):
        return "unknown"


def _alarms_firing(health: dict) -> int:
    return sum(
        1
        for a in health.get("alarms", {}).get("L", [])
        if a.get("M", {}).get("state", {}).get("S") == "ALARM"
    )


def rendered_verdicts(region: str, limit: int) -> list[tuple[str, str]]:
    """Real verdicts, rendered readably. Raw DynamoDB JSON is unreadable on a
    slide, and a screenshot of a console is not a citable artifact."""
    import boto3

    ddb = boto3.client("dynamodb", region_name=region)
    items = [
        i
        for i in ddb.scan(TableName=f"{PROJECT}-verdicts").get("Items", [])
        if _attr(i, "pipeline_execution_id")
    ]
    items.sort(key=lambda i: _attr(i, "recorded_at"), reverse=True)

    out = []
    for item in items[:limit]:
        vid = _attr(item, "verdict_id")
        concerns = item.get("verdict", {}).get("M", {}).get("primary_concerns", {}).get("L", [])
        health = (
            item.get("signals", {})
            .get("M", {})
            .get("signals", {})
            .get("M", {})
            .get("target_health", {})
            .get("M", {})
            .get("data", {})
            .get("M", {})
        )
        body = [
            f"# Verdict {vid[:8]}",
            "",
            f"_Real pipeline execution, {_attr(item, 'recorded_at')[:16]} UTC. "
            f"Exported {_stamp()} from the `{PROJECT}-verdicts` table._",
            "",
            "| | |",
            "| --- | --- |",
            f"| risk | **{_attr(item, 'verdict', 'risk_level')}** |",
            f"| action taken | {_attr(item, 'action_taken')} |",
            f"| mode | {_attr(item, 'mode')} |",
            f"| confidence | {_attr(item, 'verdict', 'confidence')} |",
            f"| source | {_attr(item, 'verdict', 'source')} |",
            f"| model | `{_attr(item, 'model_call', 'model_id')}` |",
            f"| prompt version | `{_attr(item, 'model_call', 'prompt_version')}` |",
            f"| latency | {_attr(item, 'model_call', 'latency_ms')}ms |",
            f"| tokens in / out | {_attr(item, 'model_call', 'input_tokens')} / "
            f"{_attr(item, 'model_call', 'output_tokens')} |",
            f"| commit | `{_attr(item, 'commit_sha')[:12]}` |",
            "",
        ]
        raised = _attr(item, "verdict", "floor_raised_from")
        if raised:
            body += [
                f"> **The floor raised this from `{raised}`.** "
                f"{_attr(item, 'verdict', 'floor_reason')}",
                "",
            ]
        body += [
            "## Signals",
            "",
            f"- error rate **{_pct(health.get('error_rate_pct', {}).get('N'))}** over "
            f"{health.get('invocations_last_hour', {}).get('N', '?')} invocations",
            f"- alarms firing: {_alarms_firing(health)}",
            "",
            "## Reasoning",
            "",
            _attr(item, "verdict", "reasoning"),
            "",
        ]
        # A reasoning field ending in "..." looks like a broken export. It is
        # not: the validator caps reasoning at 600 characters and RECORDS that
        # it did (D-031), so the audit trail can never quietly present a
        # shortened answer as a complete one. Say so, rather than leaving a
        # reader to wonder whether the tool lost something.
        truncated = item.get("verdict", {}).get("M", {}).get("truncated_fields", {}).get("L", [])
        if any(t.get("S") == "reasoning" for t in truncated):
            body += [
                "> Cut off above because the model exceeded the 600-character cap the",
                "> validator enforces. The truncation is recorded on the verdict, so a",
                "> shortened answer can never pass as a complete one.",
                "",
            ]
        if concerns:
            body += ["## Primary concerns", ""]
            body += [f"- {c.get('S', '')}" for c in concerns]
            body.append("")
        out.append((f"verdict-{vid[:8]}.md", "\n".join(body)))
    return out


def _count_entries(path: Path, prefix: str) -> int:
    if not path.exists():
        return 0
    lines = path.read_text(encoding="utf-8").splitlines()
    return sum(1 for line in lines if line.startswith(prefix))


def _test_count() -> str:
    """Collected, not counted by grep -- parametrised tests would be undercounted
    and the number would be quietly wrong in the direction that flatters us."""
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "pytest", "--collect-only", "-q"],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    # `-q` prints one `path/to/test_x.py: 13` line per file and, in this pytest
    # version, no grand total. Summing the per-file counts is more robust than
    # matching a summary line whose wording changes between releases -- and the
    # first attempt at this returned "unknown" for exactly that reason.
    total = 0
    files = 0
    for line in result.stdout.splitlines():
        _, _, count = line.rpartition(": ")
        if count.strip().isdigit() and line.strip().startswith("tests/"):
            total += int(count)
            files += 1
    return f"**{total}** across {files} files" if total else "unknown"


def _verdict_stats(region: str) -> dict:
    """Latency, tokens and risk distribution from real pipeline executions."""
    import boto3

    ddb = boto3.client("dynamodb", region_name=region)
    items = [
        i
        for i in ddb.scan(TableName=f"{PROJECT}-verdicts").get("Items", [])
        if _attr(i, "pipeline_execution_id")
    ]
    latencies = sorted(int(v) for i in items if (v := _attr(i, "model_call", "latency_ms")))
    tokens = [int(v) for i in items if (v := _attr(i, "model_call", "input_tokens"))]
    risks: dict[str, int] = {}
    halted = 0
    for i in items:
        risks[_attr(i, "verdict", "risk_level") or "?"] = (
            risks.get(_attr(i, "verdict", "risk_level") or "?", 0) + 1
        )
        halted += _attr(i, "action_taken") == "halt_pipeline"
    return {
        "count": len(items),
        "halted": halted,
        "risks": risks,
        "median_latency": latencies[len(latencies) // 2] if latencies else None,
        "max_latency": latencies[-1] if latencies else None,
        "mean_input_tokens": sum(tokens) // len(tokens) if tokens else None,
    }


def _spend(region: str) -> dict:
    """Real money, from Cost Explorer. Credits are reported separately because
    Bedrock's Anthropic models bill through AWS Marketplace, which most AWS
    credits exclude (D-081)."""
    import datetime

    import boto3

    ce = boto3.client("ce", region_name=region)
    end = datetime.date.today()
    start = end - datetime.timedelta(days=90)
    grouped = ce.get_cost_and_usage(
        TimePeriod={"Start": str(start), "End": str(end)},
        Granularity="MONTHLY",
        Metrics=["UnblendedCost"],
        GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
    )
    totals: dict[str, float] = {}
    for period in grouped["ResultsByTime"]:
        for group in period["Groups"]:
            amount = float(group["Metrics"]["UnblendedCost"]["Amount"])
            if amount:
                totals[group["Keys"][0]] = totals.get(group["Keys"][0], 0.0) + amount
    return {"by_service": totals, "days": 90}


def metrics(region: str, offline: bool) -> str:
    """Every number in one place, each with where it came from.

    The page to keep open while writing slides. Numbers that need AWS are
    omitted rather than guessed when `--offline`, and the file says which.
    """
    lines = [
        "# Metrics -- every number, and where it came from",
        "",
        f"_Generated {_stamp()} by `scripts/capture_artifacts.py`._",
        "",
        "## The two rates (fixture set, 23 scenarios, 3 repeats)",
        "",
        "| model | under-flagging | over-flagging | acceptable |",
        "| --- | --- | --- | --- |",
    ]
    for label, name in (
        ("Haiku 4.5", "2026-09-08-haiku-asymmetric.json"),
        ("Sonnet 4.5", "sonnet-4-5.json"),
        ("Nova Pro", "nova-pro.json"),
        ("**Sonnet 4.5 + floor**", "sonnet-4-5-with-floor.json"),
        ("arithmetic baseline", None),
    ):
        if name is None:
            lines.append("| arithmetic baseline | run `python -m evals.run --baseline` | | |")
            continue
        run = _load(name)
        if run:
            lines.append(
                f"| {label} | {_rate(run['under_flagging'])} "
                f"| {_rate(run['over_flagging'])} | {_rate(run['passes'])} |"
            )
    lines += ["", "_Source: `evals/results/*.json`._", ""]

    lines += [
        "## The build",
        "",
        f"- tests: {_test_count()}",
        f"- architectural decisions: **{_count_entries(ROOT / 'DECISIONS.md', '## D-')}**",
        f"- recorded failures: **{_count_entries(ROOT / 'FAILURES.md', '## F-')}**",
        "",
        "_Source: `DECISIONS.md`, `FAILURES.md`, `pytest --collect-only`._",
        "",
    ]

    if offline:
        lines += [
            "## Live figures",
            "",
            "_Not generated: `--offline`. Re-run without it for latency, real-deploy",
            "rates and spend._",
            "",
        ]
        return "\n".join(lines) + "\n"

    try:
        v = _verdict_stats(region)
        risks = ", ".join(f"{k} {n}" for k, n in sorted(v["risks"].items()))
        lines += [
            "## Real deploys (not fixtures)",
            "",
            f"- verdicts from real pipeline executions: **{v['count']}**",
            f"- actually halted: **{v['halted']}**",
            f"- risk distribution: {risks}",
            "",
            "This is the number that decides whether the gate is deployable. A gate that",
            "scores well on 23 fixtures and halts half your real deploys is not.",
            "",
            "## Latency and size",
            "",
            f"- median verdict latency: **{v['median_latency']}ms**",
            f"- slowest observed: {v['max_latency']}ms",
            f"- mean input tokens: {v['mean_input_tokens']:,}"
            if v["mean_input_tokens"]
            else "- mean input tokens: unknown",
            "",
            f"_Source: the `{PROJECT}-verdicts` table, {v['count']} real executions._",
            "",
        ]
    except Exception as exc:  # noqa: BLE001
        lines += [f"## Real deploys\n\n_Unavailable: {type(exc).__name__}._\n"]

    try:
        s = _spend(region)
        lines += [
            "## Cost",
            "",
            f"Last {s['days']} days, gross:",
            "",
            "| service | spend |",
            "| --- | --- |",
        ]
        for service, amount in sorted(s["by_service"].items(), key=lambda kv: -kv[1]):
            lines.append(f"| {service} | ${amount:.4f} |")
        total = sum(s["by_service"].values())
        lines += [
            f"| **total** | **${total:.4f}** |",
            "",
            "Bedrock's Anthropic models bill as an AWS **Marketplace** purchase",
            "(`USE1-MP:` usage types), which most AWS credits exclude -- so model spend",
            "is real money while the rest of the stack is largely credited (D-081).",
            "",
            "_Source: Cost Explorer, `GetCostAndUsage` grouped by SERVICE._",
            "",
        ]
    except Exception as exc:  # noqa: BLE001
        lines += [f"## Cost\n\n_Unavailable: {type(exc).__name__}. Needs `ce:GetCostAndUsage`._\n"]

    return "\n".join(lines) + "\n"


def gate_history() -> str | None:
    """Reuses the existing readout rather than reimplementing it."""
    result = subprocess.run(  # noqa: S603
        [sys.executable, str(ROOT / "scripts" / "gate_history.py")],
        capture_output=True,
        text=True,
        env={**os.environ, "NO_COLOR": "1"},
    )
    if result.returncode != 0:
        return None
    return (
        "# Gate history -- what it did to real deploys\n\n"
        f"_Generated {_stamp()} by `scripts/gate_history.py`._\n\n"
        "This is the REAL-WORLD rate, as distinct from the eval's fixture-based one.\n"
        "A gate that scores well on 23 fixtures and halts half of your actual\n"
        "deploys is not deployable, and only this number can tell you that.\n\n"
        "```\n" + result.stdout.strip() + "\n```\n"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-east-1"))
    parser.add_argument("--verdicts", type=int, default=3, help="how many verdicts to render")
    parser.add_argument("--offline", action="store_true", help="skip anything needing AWS")
    args = parser.parse_args()

    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    skipped: list[str] = []

    metrics_page = metrics(args.region, args.offline)
    (ARTIFACTS / "metrics.md").write_text(metrics_page, encoding="utf-8")
    written.append("metrics.md")

    for name, builder in (
        ("model-comparison.md", model_comparison),
        ("floor-effect.md", floor_effect),
    ):
        content = builder()
        if content:
            (ARTIFACTS / name).write_text(content, encoding="utf-8")
            written.append(name)
        else:
            skipped.append(f"{name} (missing eval result files)")

    if args.offline:
        skipped.append("verdicts and gate history (--offline)")
    else:
        try:
            for name, content in rendered_verdicts(args.region, args.verdicts):
                (ARTIFACTS / name).write_text(content, encoding="utf-8")
                written.append(name)
        except Exception as exc:  # noqa: BLE001 - a missing artifact is not a crash
            skipped.append(f"verdicts ({type(exc).__name__})")

        history = gate_history()
        if history:
            (ARTIFACTS / "gate-history.md").write_text(history, encoding="utf-8")
            written.append("gate-history.md")
        else:
            skipped.append("gate-history.md (gate_history.py failed)")

    index = [
        "# Talk artifacts",
        "",
        f"_Regenerate with `python scripts/capture_artifacts.py`. Last run {_stamp()}._",
        "",
        "Every number the talk claims should be traceable to a file here, and every",
        "file names the run or table it came from. Generated rather than written,",
        "because a number typed into a slide is one nobody can re-derive six weeks",
        "later on stage.",
        "",
        "| file | what it shows |",
        "| --- | --- |",
    ]
    descriptions = {
        "model-comparison.md": "three models, one fixture set -- the central finding",
        "floor-effect.md": "under-flagging 27.3% to 0.0%, and what the floor cannot fix",
        "gate-history.md": "what the gate did to real deploys, not fixtures",
        "metrics.md": "every number in one place, each with its source",
    }
    for name in written:
        desc = descriptions.get(name, "a real verdict, rendered readably")
        index.append(f"| [`{name}`]({name}) | {desc} |")
    if skipped:
        index += ["", "## Not generated this run", ""]
        index += [f"- {s}" for s in skipped]
    (ARTIFACTS / "README.md").write_text("\n".join(index) + "\n", encoding="utf-8")

    print(_c(f"\nwrote {len(written) + 1} artifact(s) to {ARTIFACTS}", "green"))
    for name in written:
        print(f"  {name}")
    for note in skipped:
        print(_c(f"  skipped: {note}", "dim"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
