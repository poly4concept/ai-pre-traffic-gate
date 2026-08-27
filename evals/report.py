"""Format an EvalRun for a terminal. Phase 4a.

Ordering is the design. The headline is the two failure rates, then the caveats
that decide whether those rates mean anything, then the detail. A report that
opens with a single accuracy figure invites reading only the first line, and the
first line is the least informative thing here.

`fail_closed` attempts are printed near the top, before the accuracy figures,
because a run made mostly of fail-closed verdicts is measuring error handling
rather than judgement -- and its accuracy number is meaningless. That has to be
visible before the number, not in a footnote after it.

ASCII only, and no colour unless stdout is a tty. Same reason as
check_bedrock_access.py: the Windows console codepage mangles anything outside
cp1252, and a mojibake bug during a live demo is not worth an em-dash.
"""

from __future__ import annotations

import os
import sys

from verdict import RISK_ORDER

from .harness import EvalRun
from .labels import Kind

_USE_COLOUR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")

_CODES = {"green": "32", "red": "31", "yellow": "33", "dim": "90", "bold": "1"}


def _c(text: str, colour: str) -> str:
    if not _USE_COLOUR:
        return text
    return f"\033[{_CODES[colour]}m{text}\033[0m"


def _pct(n: int, d: int) -> str:
    if d == 0:
        # Not 0% -- there was nothing to measure. The distinction this whole
        # project is about, applied to its own report.
        return "  n/a"
    return f"{100 * n / d:5.1f}%"


def _rule(char: str = "-", width: int = 78) -> str:
    return char * width


def format_run(run: EvalRun) -> str:
    out: list[str] = []
    w = out.append

    w(_rule("="))
    w(_c("EVAL RUN", "bold"))
    w(_rule("="))
    w(f"  model      {run.model_id}")
    w(f"  scenarios  {len(run.results)}  (repeats: {run.repeats})")

    # --- caveats first ---------------------------------------------------
    total_attempts = sum(len(r.attempts) for r in run.results)
    if run.fail_closed_count:
        share = _pct(run.fail_closed_count, total_attempts)
        line = (
            f"  WARNING    {run.fail_closed_count}/{total_attempts} attempts "
            f"({share.strip()}) failed closed"
        )
        w(_c(line, "yellow"))
        w("             A run made largely of fail-closed verdicts measures error")
        w("             handling, not judgement. Treat the rates below with care.")

    stable, total = run.stability
    if stable != total:
        w(
            _c(
                f"  WARNING    {total - stable}/{total} scenarios answered inconsistently "
                "across repeats",
                "yellow",
            )
        )

    # --- the two numbers -------------------------------------------------
    w("")
    w(_rule("="))
    w(_c("THE TWO RATES", "bold"))
    w(_rule("="))
    over_n, over_d = run.over_flagging
    under_n, under_d = run.under_flagging

    w(f"  UNDER-flagging   {_pct(under_n, under_d)}   ({under_n}/{under_d})")
    w("      risky changes waved through. Decides whether the gate is worth having.")
    w(f"  OVER-flagging    {_pct(over_n, over_d)}   ({over_n}/{over_d})")
    w("      safe changes blocked. Decides whether the gate survives its colleagues.")
    w("")
    w("  Reported separately and never averaged: they need opposite fixes, and a")
    w("  combined figure hides which one is happening.")

    # --- supporting ------------------------------------------------------
    w("")
    w(_rule())
    w("Supporting figures")
    w(_rule())
    p_n, p_d = run.passes
    e_n, e_d = run.exact
    w(f"  acceptable verdict     {_pct(p_n, p_d)}   ({p_n}/{p_d} scored scenarios)")
    w(f"  exactly the ideal      {_pct(e_n, e_d)}   ({e_n}/{e_d})  -- interesting, not a target")
    w(f"  stable across repeats  {_pct(stable, total)}   ({stable}/{total})")
    ins, outs = run.tokens
    w(f"  tokens                 in={ins:,} out={outs:,} over {total_attempts} attempts")
    if total_attempts:
        w(
            f"                         mean {ins // total_attempts:,} in / "
            f"{outs // total_attempts:,} out per verdict"
        )

    # --- per scenario ----------------------------------------------------
    w("")
    w(_rule("="))
    w(_c("PER SCENARIO", "bold"))
    w(_rule("="))
    w(f"  {'scenario':38} {'kind':14} {'got':7} {'want':16} concerns")
    w(f"  {_rule('-', 38)} {_rule('-', 14)} {_rule('-', 7)} {_rule('-', 16)} --------")

    for result in sorted(run.results, key=lambda r: (str(r.label.kind), r.scenario)):
        label = result.label
        got = str(result.modal_level)
        want = "/".join(str(a) for a in sorted(label.acceptable, key=lambda x: RISK_ORDER[x]))
        if not label.scored:
            mark, colour = "~", "dim"
        elif result.passed:
            mark, colour = "+", "green"
        else:
            mark, colour = "!", "red"

        cov = result.concern_coverage
        cov_text = "   -" if cov is None else f"{100 * cov:3.0f}%"
        if not result.is_stable:
            got = got + "*"

        line = f"{mark} {result.scenario:38} {str(label.kind):14} {got:7} {want:16} {cov_text}"
        w("  " + _c(line, colour))

    w("")
    w("  + acceptable   ! failure   ~ contested (recorded, not scored)")
    w("  * risk level varied across repeats")
    w("  concerns = share of expected keywords present in the reasoning; reported,")
    w("             never scored, because keyword matching on prose is brittle")

    # --- failures --------------------------------------------------------
    if run.failures:
        w("")
        w(_rule("="))
        w(_c(f"FAILURES ({len(run.failures)})", "bold"))
        w(_rule("="))
        for result in run.failures:
            direction = result.direction or "?"
            arrow = "TOO STRICT" if direction == "over" else "TOO LENIENT"
            w("")
            w(_c(f"  [{arrow}] {result.scenario}", "red"))
            allowed = sorted(str(a) for a in result.label.acceptable)
            w(f"    got {result.modal_level}, acceptable: {allowed}")
            w(f"    levels across repeats: {[str(x) for x in result.levels]}")
            reasoning = result.attempts[0].reasoning
            w(f"    model said: {reasoning[:200]}")

    # --- model-call violations -------------------------------------------
    if run.model_call_violations:
        w("")
        w(_rule("="))
        w(_c("STRUCTURAL VIOLATIONS", "bold"))
        w(_rule("="))
        for result in run.model_call_violations:
            w(
                _c(
                    f"  {result.scenario}: the model WAS called on a scenario that must "
                    "never reach it",
                    "red",
                )
            )
            w("    A model asked to judge a change it was never shown returns a")
            w("    confident, invented verdict. This must be refused before inference.")

    # --- pair violations -------------------------------------------------
    w("")
    w(_rule("="))
    w(_c(f"ORDERING CHECKS ({len(run.pair_violations)} violations)", "bold"))
    w(_rule("="))
    if not run.pair_violations:
        w(_c("  all ordering assertions held", "green"))
    for v in run.pair_violations:
        w("")
        w(_c(f"  {v.lax} ({v.lax_level}) ranked above {v.strict} ({v.strict_level})", "red"))
        w(f"    {v.why}")
    w("")
    w("  Ordering survives calibration differences that absolute labels do not:")
    w("  a uniformly more cautious model fails many labels while staying")
    w("  self-consistent. A violation here is a reasoning error, not taste.")

    # --- contested -------------------------------------------------------
    contested = [r for r in run.results if r.label.kind is Kind.CONTESTED]
    if contested:
        w("")
        w(_rule("="))
        w(_c("CONTESTED (recorded, not scored)", "bold"))
        w(_rule("="))
        for result in contested:
            counts: dict[str, int] = {}
            for level in result.levels:
                counts[str(level)] = counts.get(str(level), 0) + 1
            spread = ", ".join(
                f"{k}={v}" for k, v in sorted(counts.items(), key=lambda kv: RISK_ORDER[kv[0]])
            )
            w(f"  {result.scenario}: {spread}")
        w("")
        w("  No label, on purpose. Inventing one would bake a guess into the")
        w("  benchmark and then score the model on matching it.")

    w("")
    return "\n".join(out)
