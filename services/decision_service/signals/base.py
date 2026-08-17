"""The collector interface, and the fail-closed guarantee built into it.

Phase 2.1.

THE CENTRAL IDEA OF THIS FILE, and probably of Phase 2:

    An absent signal is not a negative signal.

"Amazon Inspector reported no findings" and "Amazon Inspector was unreachable"
must never produce the same value. If they do, a broken collector looks exactly
like a clean bill of health, and the gate fails OPEN through the back door --
not through a permissive decision, but through a plausible-looking zero.

This is the subtlest way a fail-closed system can be defeated, because nothing
errors, nothing logs a warning, and the verdict reads as confident. Every
collector therefore returns a `SignalResult` carrying an explicit status, and
`data` is `None` whenever that status is not OK. There is no default-valued
`SecurityFindings()` waiting to be mistaken for evidence.

The second idea: collectors CANNOT FORGET TO FAIL CLOSED, because they do not
implement the failure path. `collect()` is concrete on the base class and wraps
`_collect()` in timing and exception handling. A subclass author writes only the
happy path. This is the template method pattern earning its keep -- correctness
that cannot be omitted by a future contributor who has not read this comment.
"""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import StrEnum

logger = logging.getLogger(__name__)


class SignalStatus(StrEnum):
    """Whether a signal can be trusted, and if not, why not."""

    OK = "ok"
    """Collected successfully. `data` is populated."""

    UNAVAILABLE = "unavailable"
    """Collection failed. `data` is None. The gate knows nothing here."""

    DEGRADED = "degraded"
    """Partial data. Something was collected but it is known incomplete --
    a truncated page, a metric window with gaps. Kept distinct from OK because
    the verdict layer should weigh it differently, and distinct from
    UNAVAILABLE because throwing away real information is also a loss."""

    SKIPPED = "skipped"
    """Deliberately not collected -- disabled by configuration. Distinct from
    UNAVAILABLE because it is an intended state, not a fault, and should not
    read as an incident."""


@dataclass(frozen=True)
class SignalResult[T]:
    """A signal, or an honest account of why there isn't one."""

    collector: str
    status: SignalStatus
    data: T | None = None
    error: str | None = None
    duration_ms: float = 0.0

    def __post_init__(self) -> None:
        """Enforce the invariant this whole module exists to protect.

        Checked at runtime rather than left to convention. A collector that
        returns OK with no data, or UNAVAILABLE with data attached, is a bug
        that would otherwise surface much later as a confidently wrong verdict.
        """
        if self.status is SignalStatus.OK and self.data is None:
            raise ValueError(f"{self.collector}: status OK but data is None")
        if self.status is SignalStatus.UNAVAILABLE and self.data is not None:
            raise ValueError(f"{self.collector}: status UNAVAILABLE but data was supplied")

    @property
    def is_usable(self) -> bool:
        """True if there is data worth showing the verdict layer."""
        return self.status in (SignalStatus.OK, SignalStatus.DEGRADED) and self.data is not None


class SignalCollector[T](ABC):
    """Base class for every collector, mock and real alike.

    Subclasses implement `_collect()` and nothing else. They may raise freely;
    raising is a normal way to report that a signal could not be obtained.
    """

    name: str = "unnamed"

    # Real collectors pass this to their boto3 client config. Enforcing a
    # timeout in-process (signal.alarm, threads) is unreliable inside Lambda and
    # would only duplicate what the SDK already does properly at the socket
    # layer. Declared on the base class so every collector has one and none has
    # to remember to.
    timeout_seconds: float = 5.0

    @abstractmethod
    def _collect(self) -> T:
        """Fetch the signal. Raise on failure; do not catch and return a default.

        The temptation to return `SecurityFindings()` on error is exactly the
        failure this module is designed to prevent. Raise, and the base class
        will record UNAVAILABLE with no data.
        """

    def collect(self) -> SignalResult[T]:
        """Run `_collect()` with timing and fail-closed error handling."""
        started = time.perf_counter()
        try:
            data = self._collect()
        except Exception as exc:
            duration_ms = (time.perf_counter() - started) * 1000
            # Deliberately broad. A collector raising something unanticipated is
            # precisely the case that must not fall through to a default value,
            # and narrowing this to expected exception types would recreate the
            # "only handles failures you thought of" flaw called out in D-014.
            #
            # The exception TYPE is included because the message alone often is
            # not enough to tell a throttle from a permissions problem -- a
            # lesson from F-004, where three unrelated gates shared one error
            # code.
            reason = f"{type(exc).__name__}: {exc}"
            logger.warning("collector %s failed: %s", self.name, reason, exc_info=True)
            return SignalResult(
                collector=self.name,
                status=SignalStatus.UNAVAILABLE,
                data=None,
                error=reason,
                duration_ms=duration_ms,
            )

        duration_ms = (time.perf_counter() - started) * 1000
        return SignalResult(
            collector=self.name,
            status=SignalStatus.OK,
            data=data,
            duration_ms=duration_ms,
        )


class DisabledCollector[T](SignalCollector[T]):
    """Stands in for a collector switched off by configuration.

    Returns SKIPPED rather than UNAVAILABLE. The distinction matters
    operationally: "we chose not to look" should not page anyone, while "we
    tried to look and could not" might. Both still deny the verdict layer the
    signal, and neither is allowed to look like good news.
    """

    def __init__(self, name: str, reason: str = "disabled by configuration") -> None:
        self.name = name
        self._reason = reason

    def _collect(self) -> T:  # pragma: no cover - never reached
        raise NotImplementedError

    def collect(self) -> SignalResult[T]:
        return SignalResult(
            collector=self.name,
            status=SignalStatus.SKIPPED,
            data=None,
            error=self._reason,
        )
