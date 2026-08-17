"""Normalized shapes for everything the gate knows about a deployment.

Phase 2.1. These are the *outputs* of collection, deliberately defined before any
collector exists, because the shape of what the gate knows is a design decision
and not a byproduct of whichever AWS API happened to be convenient.

Three signal domains, in the order CLAUDE.md ranks them by difficulty:

    ChangeContext    what is being deployed
    SecurityFindings what is known to be wrong with it
    TargetHealth     what state the thing being deployed into is in

Everything is frozen. A signal bundle is evidence about a moment in time; if
some later stage could mutate it, the audit record would no longer be a record
of what the decision was actually made from.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum


class Provenance(StrEnum):
    """Where a fact came from, and therefore how much it can be trusted.

    Phase 2.2. Signals are not equally reliable, and collapsing that distinction
    loses something the verdict layer and the audit trail both need.

    The gate's input is partly attacker-influenced on any repository that
    accepts pull requests. Commit messages and paths are obviously so. Less
    obviously, so is anything computed by `buildspec.yml`, because that file
    lives in the repository being judged -- a change can rewrite the rules used
    to measure it, and report a 5,000-line diff as three lines.

    That is a different attack from prompt injection and arguably a worse one:
    injected prose is at least visible in the audit record, while a forged
    integer looks exactly like a real one and quietly corrupts Phase 4's
    measurements too.

    Recording provenance does not prevent any of this. It makes it visible.
    """

    PIPELINE = "pipeline"
    """CodePipeline read it from the source connection. No repository file was
    involved, so a commit cannot alter it."""

    BUILD = "build"
    """Computed during the build, by a script that lives in the repository.
    Self-reported by the thing being judged."""

    AWS_API = "aws_api"
    """Read from an AWS control-plane API. Outside the repository's reach."""

    MOCK = "mock"
    """Fixture data. Not from anywhere."""

    NONE = "none"
    """No value was collected. Distinct from a collected zero -- the same
    absent-is-not-negative rule the whole package turns on, applied to a single
    field group rather than to a whole signal."""


class Severity(StrEnum):
    """Severity ladder, ordered. StrEnum so it serialises as a plain string."""

    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFORMATIONAL = "informational"

    UNKNOWN = "unknown"
    """Severity could not be determined.

    Phase 2.3. Amazon Inspector emits `UNTRIAGED` for findings it has not yet
    scored, and may add severity values we have never seen. Neither can be
    mapped to a number.

    The temptation is to fold those into LOW or INFORMATIONAL, which is quiet
    and wrong -- an unscored critical vulnerability would vanish into the noise
    floor. Mapping them upward to CRITICAL is equally wrong and would make the
    gate cry wolf. So they get their own value and stay visible: a bundle
    reporting "3 critical, 1 unknown" is telling the truth, where one reporting
    "3 critical, 1 low" is not.

    Same rule as everything else here -- do not let something you could not
    determine masquerade as something reassuring."""


@dataclass(frozen=True)
class SecurityFinding:
    """One vulnerability, normalised away from any particular scanner's shape."""

    id: str
    severity: Severity
    title: str
    package: str | None = None
    installed_version: str | None = None
    fixed_version: str | None = None

    @property
    def is_fixable(self) -> bool:
        """A finding with a fix available is a different decision from one without.

        "Critical, patch exists, you chose not to apply it" and "critical, no
        patch exists anywhere" are the same severity and completely different
        judgements. Collapsing them into a severity count loses the distinction,
        so the verdict layer gets it explicitly.
        """
        return self.fixed_version is not None


@dataclass(frozen=True)
class ChangeContext:
    """What is being deployed, and in what circumstances.

    Every string field here is ATTACKER-INFLUENCED on any repository that
    accepts pull requests -- commit messages, branch names and file paths are
    all writable by whoever opens the PR. From Phase 3 this text reaches a
    language model. That is the assumption the whole permission boundary in
    D-016 is built around, and it is why these stay as data rather than being
    interpolated into anything early.
    """

    commit_sha: str
    commit_message: str
    branch: str
    author: str
    committed_at: datetime

    files_changed: int
    lines_added: int
    lines_removed: int
    paths: tuple[str, ...] = ()

    # Deploy cadence. A tenth deploy in an hour is a different risk profile from
    # the first in a fortnight, in both directions: rapid successive deploys can
    # mean an unfolding incident, and a long gap means a large accumulated delta
    # and cold operational muscle memory.
    # Optional, and `None` rather than `0` when unknown. Nothing in the build
    # can see deployment history -- that lives in the CodeDeploy control plane --
    # so a build-sourced change context legitimately has no value here. Zero
    # deploys in 24 hours is a real and meaningful state (a quiet service), and
    # must not double as "we did not look".
    deploys_last_24h: int | None = None
    hours_since_last_deploy: float | None = None

    # Where each group of facts came from. Default MOCK because the hand-built
    # scenarios genuinely are mocks -- a fixture claiming pipeline provenance
    # would be lying about itself.
    metadata_provenance: Provenance = Provenance.MOCK
    diff_provenance: Provenance = Provenance.MOCK
    cadence_provenance: Provenance = Provenance.MOCK

    @property
    def self_reported_fields(self) -> tuple[str, ...]:
        """Field groups the change described about itself.

        Phase 3 puts this in the prompt. A model told that diff statistics are
        self-reported can treat a suspiciously small diff differently from one
        it has reason to trust -- and, more usefully, its reasoning will say so,
        which puts the caveat into the audit record rather than only into a
        design document nobody rereads.
        """
        groups = {
            "commit metadata": self.metadata_provenance,
            "diff statistics": self.diff_provenance,
            "deploy cadence": self.cadence_provenance,
        }
        return tuple(name for name, source in groups.items() if source is Provenance.BUILD)

    @property
    def total_lines_changed(self) -> int:
        return self.lines_added + self.lines_removed

    @property
    def is_off_hours(self) -> bool:
        """Deploy outside 08:00-18:00 Mon-Fri, in the commit's own timezone.

        Deliberately computed here rather than left for the model to infer from
        a timestamp. Anything a deterministic function can decide should not be
        delegated to a probabilistic one -- the model cannot get this wrong if
        it is never asked, and Phase 4 can assert on it.
        """
        if self.committed_at.weekday() >= 5:
            return True
        return not (8 <= self.committed_at.hour < 18)


@dataclass(frozen=True)
class SecurityFindings:
    """Security posture of the artifact being deployed."""

    findings: tuple[SecurityFinding, ...] = ()
    scanned_at: datetime | None = None
    scanner: str = "unknown"

    # What was actually scanned, e.g. "ai-pre-traffic-gate-demo-app:4".
    scan_target: str = ""

    # THE CAVEAT THAT MAKES THIS SIGNAL HONEST, and it is a big one.
    #
    # Amazon Inspector scans *deployed* resources. The gate runs *before* the
    # deploy. So findings collected at gate time describe the version currently
    # live, NOT the candidate about to replace it.
    #
    # That means the obvious signal source answers a subtly different question
    # than the one asked. "Is this change safe?" is not what Inspector is
    # reporting on; "is the thing this change would replace currently known to
    # be vulnerable?" is. Still useful -- deploying into a service with active
    # criticals is real context, and a change that fixes them is a point in its
    # favour -- but it is not vulnerability data about the new code.
    #
    # Scanning the candidate artifact requires a different mechanism entirely
    # (an SBOM generated in the build and scanned before deploy). Recorded as a
    # flag rather than a comment so the verdict layer and the audit record can
    # both see which question was answered.
    is_candidate_artifact: bool = False

    @property
    def unknown_count(self) -> int:
        return self.count_by_severity(Severity.UNKNOWN)

    def count_by_severity(self, severity: Severity) -> int:
        return sum(1 for f in self.findings if f.severity is severity)

    @property
    def critical_count(self) -> int:
        return self.count_by_severity(Severity.CRITICAL)

    @property
    def high_count(self) -> int:
        return self.count_by_severity(Severity.HIGH)

    @property
    def fixable_critical_or_high(self) -> tuple[SecurityFinding, ...]:
        return tuple(
            f
            for f in self.findings
            if f.is_fixable and f.severity in (Severity.CRITICAL, Severity.HIGH)
        )


@dataclass(frozen=True)
class Alarm:
    name: str
    state: str
    reason: str = ""


@dataclass(frozen=True)
class TargetHealth:
    """Live state of the environment being deployed INTO.

    This is the signal a test suite structurally cannot provide, and the reason
    the project exists. Tests tell you the code is fine. They cannot tell you
    the target is currently on fire.
    """

    error_rate_pct: float
    p99_latency_ms: float
    invocations_last_hour: int
    alarms: tuple[Alarm, ...] = ()
    window_minutes: int = 60

    @property
    def alarms_in_alarm(self) -> tuple[Alarm, ...]:
        return tuple(a for a in self.alarms if a.state == "ALARM")

    @property
    def has_active_alarm(self) -> bool:
        return bool(self.alarms_in_alarm)

    @property
    def is_low_traffic(self) -> bool:
        """Low traffic makes every other health number statistically meaningless.

        The threshold is arbitrary and deliberately conservative. The point is
        not the exact number -- it is that "0% error rate" over 3 requests and
        over 300,000 requests are not the same claim, and a verdict that treats
        them alike is reading noise. Phase 1's canary sampling taught this
        lesson concretely (DECISIONS.md D-019); it applies to every rate in
        this class.
        """
        return self.invocations_last_hour < 100


@dataclass(frozen=True)
class DeploymentTarget:
    """Which thing is being deployed. Identifies what health refers to."""

    service_name: str
    environment: str = "personal"
    region: str = "us-east-1"
    metadata: dict[str, str] = field(default_factory=dict)
