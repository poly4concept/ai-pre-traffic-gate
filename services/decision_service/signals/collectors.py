"""Per-domain collector base classes, and the mock implementation of each.

Phase 2.1 -- mocks only. The real implementations arrive in 2.2 (change
context), 2.3 (Inspector) and 2.4 (CloudWatch), in that order because that is
increasing order of difficulty, and each will sit beside its mock in this file's
place in the package.

Why mocks first, from CLAUDE.md constraint 5: the whole flow must run end to end
with zero real AWS signal dependencies. Development must not be blocked on
Inspector onboarding or on generating production traffic, Phase 4's fixtures
need signals that never change, and tests must run offline.

Each mock is a plain constructor argument holding a prepared value. There is no
clever generation, no randomness, and no clock -- a mock that varies is not a
fixture, and Phase 4 needs to feed identical input twice and compare verdicts
(constraint 6).
"""

from __future__ import annotations

from .base import SignalCollector
from .types import ChangeContext, SecurityFindings, TargetHealth

# --- Domain base classes --------------------------------------------------
#
# These exist so that call sites depend on "a thing that returns change
# context" rather than on a concrete class. Swapping mock for real in Phase 2.2
# is then a wiring change in one place, not an edit to the decision service.


class ChangeContextCollector(SignalCollector[ChangeContext]):
    name = "change_context"


class SecurityFindingsCollector(SignalCollector[SecurityFindings]):
    name = "security_findings"


class TargetHealthCollector(SignalCollector[TargetHealth]):
    name = "target_health"


# --- Mocks ----------------------------------------------------------------


class MockChangeContextCollector(ChangeContextCollector):
    """Returns a prepared ChangeContext, or raises a prepared exception."""

    def __init__(self, context: ChangeContext | None = None, raises: Exception | None = None):
        self._context = context
        self._raises = raises

    def _collect(self) -> ChangeContext:
        if self._raises is not None:
            raise self._raises
        if self._context is None:
            raise ValueError("MockChangeContextCollector was given nothing to return")
        return self._context


class MockSecurityFindingsCollector(SecurityFindingsCollector):
    def __init__(self, findings: SecurityFindings | None = None, raises: Exception | None = None):
        self._findings = findings
        self._raises = raises

    def _collect(self) -> SecurityFindings:
        if self._raises is not None:
            raise self._raises
        if self._findings is None:
            raise ValueError("MockSecurityFindingsCollector was given nothing to return")
        return self._findings


class MockTargetHealthCollector(TargetHealthCollector):
    def __init__(self, health: TargetHealth | None = None, raises: Exception | None = None):
        self._health = health
        self._raises = raises

    def _collect(self) -> TargetHealth:
        if self._raises is not None:
            raise self._raises
        if self._health is None:
            raise ValueError("MockTargetHealthCollector was given nothing to return")
        return self._health


# The `raises` argument is not an afterthought for tidiness -- it is half the
# point of having mocks at all.
#
# The failure paths are the ones that most need exercising and are hardest to
# produce on demand against real AWS. Throttling Inspector, or arranging for
# CloudWatch to time out, is genuinely difficult to do deliberately. A mock that
# can only succeed would leave the entire fail-closed apparatus untested until
# it fired for the first time in production, which is the worst possible moment
# to discover that "unavailable" was being read as "clean".
