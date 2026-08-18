"""Named scenarios: fixed signal bundles with known characteristics.

Phase 2.1, and the seed of the Phase 4 eval harness.

Each scenario is a hand-built situation with an obvious intuitive risk level, so
that a verdict layer can later be measured against something other than
opinion. They are defined here rather than in the test suite because Phase 4
needs them, the eventual demo needs them, and a fixture that lives in one test
file tends to get quietly edited to make that test pass.

DELIBERATELY NO EXPECTED VERDICTS YET. Labelling these `low`/`medium`/`high` is
a Phase 4 task and needs care -- a fixture set labelled casually becomes a
benchmark that rewards the prompt for agreeing with whatever was assumed on the
afternoon it was written. Phase 4 also needs the disagreements to be
interesting, which means the labels want thought, not reflex.

Every timestamp is fixed. Nothing here calls `datetime.now()`.
"""

from __future__ import annotations

from datetime import UTC, datetime

from .types import (
    Alarm,
    ChangeContext,
    DeploymentTarget,
    SecurityFinding,
    SecurityFindings,
    Severity,
    TargetHealth,
)

DEMO_TARGET = DeploymentTarget(
    service_name="ai-pre-traffic-gate-demo-app",
    environment="personal",
    region="us-east-1",
)

# Fixed reference points. Tuesday mid-morning is the calmest plausible moment to
# deploy; Friday evening is the folk-canonical worst one.
TUESDAY_MORNING = datetime(2026, 8, 11, 10, 14, tzinfo=UTC)
FRIDAY_EVENING = datetime(2026, 8, 14, 19, 42, tzinfo=UTC)
SATURDAY_NIGHT = datetime(2026, 8, 15, 23, 5, tzinfo=UTC)


# --- Change contexts ------------------------------------------------------

SAFE_DEPENDENCY_BUMP = ChangeContext(
    commit_sha="a1b2c3d4e5f60718293a4b5c6d7e8f9012345678",
    commit_message="Bump requests from 2.31.0 to 2.32.3",
    branch="main",
    author="dependabot[bot]",
    committed_at=TUESDAY_MORNING,
    files_changed=1,
    lines_added=1,
    lines_removed=1,
    paths=("requirements.txt",),
    deploys_last_24h=1,
    hours_since_last_deploy=26.5,
)

CONFIG_ONLY_CHANGE = ChangeContext(
    commit_sha="b2c3d4e5f60718293a4b5c6d7e8f901234567890",
    commit_message="Raise log retention from 7 to 14 days",
    branch="main",
    author="mubaraka",
    committed_at=TUESDAY_MORNING,
    files_changed=1,
    lines_added=1,
    lines_removed=1,
    paths=("infra/personal/demo_app.tf",),
    deploys_last_24h=2,
    hours_since_last_deploy=4.0,
)

RISKY_PAYMENTS_CHANGE = ChangeContext(
    commit_sha="c3d4e5f60718293a4b5c6d7e8f90123456789012",
    commit_message="Refactor settlement retry logic",
    branch="main",
    author="mubaraka",
    committed_at=FRIDAY_EVENING,
    files_changed=14,
    lines_added=612,
    lines_removed=445,
    paths=(
        "services/payments/settlement.py",
        "services/payments/retry.py",
        "services/payments/ledger.py",
        "services/auth/tokens.py",
    ),
    deploys_last_24h=1,
    hours_since_last_deploy=71.0,
)

HUGE_REFACTOR = ChangeContext(
    commit_sha="d4e5f60718293a4b5c6d7e8f9012345678901234",
    commit_message="Split monolith handler into modules; no behaviour change",
    branch="main",
    author="mubaraka",
    committed_at=TUESDAY_MORNING,
    files_changed=87,
    lines_added=3410,
    lines_removed=3102,
    paths=("services/demo_app/handler.py", "services/demo_app/routes.py"),
    deploys_last_24h=0,
    hours_since_last_deploy=192.0,
)

# Rapid successive deploys usually mean someone is chasing a problem. The change
# itself is tiny; the CONTEXT is what carries the risk, which is exactly the
# distinction a test suite cannot make and this gate is supposed to.
FIREFIGHTING_HOTFIX = ChangeContext(
    commit_sha="e5f60718293a4b5c6d7e8f901234567890123456",
    commit_message="fix attempt 4 - revert the revert, add null guard",
    branch="main",
    author="mubaraka",
    committed_at=SATURDAY_NIGHT,
    files_changed=1,
    lines_added=3,
    lines_removed=1,
    paths=("services/demo_app/handler.py",),
    deploys_last_24h=7,
    hours_since_last_deploy=0.4,
)


# --- Security findings ----------------------------------------------------

NO_FINDINGS = SecurityFindings(
    findings=(),
    scanned_at=TUESDAY_MORNING,
    scanner="inspector-mock",
)

CRITICAL_FIXABLE_CVE = SecurityFindings(
    findings=(
        SecurityFinding(
            id="CVE-2024-99999",
            severity=Severity.CRITICAL,
            title="Remote code execution in example-lib deserialisation",
            package="example-lib",
            installed_version="1.2.0",
            fixed_version="1.2.1",
        ),
        SecurityFinding(
            id="CVE-2024-88888",
            severity=Severity.MEDIUM,
            title="Regular expression denial of service in parser",
            package="example-parser",
            installed_version="0.9.0",
            fixed_version="0.9.4",
        ),
    ),
    scanned_at=TUESDAY_MORNING,
    scanner="inspector-mock",
)

# Same severity as the above, no patch available. The correct response differs
# completely -- there is nothing to apply, so the only lever is compensating
# controls or accepting the risk. A verdict layer that treats these
# identically is not reading the signal, only counting it.
CRITICAL_UNFIXABLE_CVE = SecurityFindings(
    findings=(
        SecurityFinding(
            id="CVE-2024-77777",
            severity=Severity.CRITICAL,
            title="Unpatched authentication bypass in abandoned-lib",
            package="abandoned-lib",
            installed_version="3.0.1",
            fixed_version=None,
        ),
    ),
    scanned_at=TUESDAY_MORNING,
    scanner="inspector-mock",
)


# --- Target health --------------------------------------------------------

HEALTHY_TARGET = TargetHealth(
    error_rate_pct=0.02,
    p99_latency_ms=180.0,
    invocations_last_hour=42_000,
    alarms=(Alarm(name="demo-app-errors", state="OK"),),
)

TARGET_IN_ALARM = TargetHealth(
    error_rate_pct=7.4,
    p99_latency_ms=3200.0,
    invocations_last_hour=38_500,
    alarms=(
        Alarm(
            name="demo-app-errors",
            state="ALARM",
            reason="Threshold Crossed: 5 datapoints were greater than 1.0",
        ),
        Alarm(name="demo-app-latency", state="ALARM", reason="p99 above 2000ms"),
    ),
)

# The trap case. Every number looks perfect, and none of them mean anything at
# 12 invocations an hour -- a 0% error rate over 12 requests is not evidence of
# health. The same arithmetic that made the canary look broken in Phase 1
# (DECISIONS.md D-019) applies to every rate the gate is shown, and a verdict
# layer that reads this as "healthy" has been fooled by a small denominator.
QUIET_TARGET = TargetHealth(
    error_rate_pct=0.0,
    p99_latency_ms=95.0,
    invocations_last_hour=12,
    alarms=(Alarm(name="demo-app-errors", state="INSUFFICIENT_DATA"),),
)


# No traffic at all, and no alarms watching. The state the real demo app was in
# when the CloudWatch collector was written, and the one that most tempts a
# verdict layer into a false positive: nothing looks wrong because nothing is
# measurable. `error_rate_pct` is None rather than 0.0 -- a ratio with a zero
# denominator is undefined, not zero (DECISIONS.md D-026).
#
# The honest reading is "we have no evidence about this target either way",
# which is a materially different input from "this target is healthy".
IDLE_UNMONITORED_TARGET = TargetHealth(
    error_rate_pct=None,
    p99_latency_ms=None,
    invocations_last_hour=0,
    alarms=(),
    has_alarm_coverage=False,
)

# --- Named combinations ---------------------------------------------------

SCENARIOS: dict[str, dict[str, object]] = {
    "safe_dependency_bump": {
        "change": SAFE_DEPENDENCY_BUMP,
        "security": NO_FINDINGS,
        "health": HEALTHY_TARGET,
        "note": "The archetypal boring deploy. Over-flagging this is the failure "
        "mode that gets a gate switched off.",
    },
    "config_only_change": {
        "change": CONFIG_ONLY_CHANGE,
        "security": NO_FINDINGS,
        "health": HEALTHY_TARGET,
        "note": "Small and dull, but touches infrastructure rather than app code.",
    },
    "risky_payments_friday": {
        "change": RISKY_PAYMENTS_CHANGE,
        "security": NO_FINDINGS,
        "health": HEALTHY_TARGET,
        "note": "Large diff, sensitive paths, off-hours. Nothing is technically "
        "wrong, which is the point -- the risk is entirely contextual.",
    },
    "friday_deploy_into_active_alarm": {
        "change": RISKY_PAYMENTS_CHANGE,
        "security": NO_FINDINGS,
        "health": TARGET_IN_ALARM,
        "note": "The scenario the whole project exists for. Tests pass; the "
        "target is actively unhealthy right now.",
    },
    "huge_refactor_healthy_target": {
        "change": HUGE_REFACTOR,
        "security": NO_FINDINGS,
        "health": HEALTHY_TARGET,
        "note": "Enormous diff, benign intent. Tests whether size alone drives the verdict.",
    },
    "critical_cve_with_patch": {
        "change": SAFE_DEPENDENCY_BUMP,
        "security": CRITICAL_FIXABLE_CVE,
        "health": HEALTHY_TARGET,
        "note": "A fix exists and was not applied.",
    },
    "critical_cve_no_patch": {
        "change": SAFE_DEPENDENCY_BUMP,
        "security": CRITICAL_UNFIXABLE_CVE,
        "health": HEALTHY_TARGET,
        "note": "Same severity, no remedy available. Should not read the same as the case above.",
    },
    "firefighting_hotfix": {
        "change": FIREFIGHTING_HOTFIX,
        "security": NO_FINDINGS,
        "health": TARGET_IN_ALARM,
        "note": "Three-line change, seventh deploy today, Saturday night, target "
        "in alarm. Blocking it may be exactly wrong -- this is plausibly the fix. "
        "Included because the honest answer is contested.",
    },
    "quiet_target_looks_healthy": {
        "change": SAFE_DEPENDENCY_BUMP,
        "security": NO_FINDINGS,
        "health": QUIET_TARGET,
        "note": "Health metrics are pristine and statistically meaningless.",
    },
    "no_health_evidence_at_all": {
        "change": RISKY_PAYMENTS_CHANGE,
        "security": NO_FINDINGS,
        "health": IDLE_UNMONITORED_TARGET,
        "note": "A large off-hours change into a target with zero traffic and no "
        "alarms. Nothing looks wrong because nothing is measurable. Tests whether "
        "the verdict layer can tell 'no evidence of problems' from 'evidence of "
        "no problems' -- the distinction the whole signals package exists for.",
    },
}
