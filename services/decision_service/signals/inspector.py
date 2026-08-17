"""Real security findings from Amazon Inspector. Phase 2.3.

THE ENTIRE REASON THIS FILE IS LONGER THAN IT LOOKS LIKE IT SHOULD BE:

    aws inspector2 list-findings   ->   {"findings": []}

That was the actual response from this account. Not an error. Not a warning. An
empty list.

Amazon Inspector was **DISABLED** — every scan type, never switched on. And the
API for "show me the vulnerabilities" answered "here are none", which is the
same response it gives for a thoroughly scanned resource with a clean bill of
health.

A naive collector is three lines long, returns `SecurityFindings(findings=())`,
and tells the verdict layer that a service which has never been scanned in its
life has zero known vulnerabilities. Nothing errors. Nothing warns. The verdict
reads as confident, and it is worse than having no security signal at all —
because "no signal" prompts a question and "no findings" closes it.

This is the absent-versus-negative rule from `base.py` in its most dangerous
real-world form, and it was found by running the call rather than by reasoning
about the API.

So this collector refuses to interpret an empty list until it has established,
by separate API calls, that somebody was actually looking:

    1. BatchGetAccountStatus  — is Inspector enabled, and is Lambda scanning on?
    2. ListCoverage           — is THIS function actually covered and scanned?
    3. ListFindings           — only now does an empty list mean "clean".

Any of those failing is UNAVAILABLE, not zero.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from .base import PartialSignal
from .collectors import SecurityFindingsCollector
from .types import SecurityFinding, SecurityFindings, Severity

logger = logging.getLogger(__name__)

# Inspector severity strings mapped to ours. UNTRIAGED means Inspector has not
# scored it yet; it maps to UNKNOWN rather than being quietly rounded down.
SEVERITY_MAP = {
    "CRITICAL": Severity.CRITICAL,
    "HIGH": Severity.HIGH,
    "MEDIUM": Severity.MEDIUM,
    "LOW": Severity.LOW,
    "INFORMATIONAL": Severity.INFORMATIONAL,
    "UNTRIAGED": Severity.UNKNOWN,
}

# Cap on findings carried into a verdict. A function with 400 findings would
# blow any prompt budget, and the first 50 by severity carry nearly all the
# signal. Exceeding it produces a DEGRADED result that says so, rather than a
# silently truncated list — the same reasoning as `paths_omitted` in the change
# context payload.
MAX_FINDINGS = 50

# Scan statuses that mean the resource is genuinely being watched. Anything else
# — including the cheerfully ambiguous ones — is treated as not covered.
HEALTHY_SCAN_STATUSES = frozenset({"ACTIVE"})


class InspectorNotEnabledError(RuntimeError):
    """Inspector, or the relevant scan type, is switched off for this account."""


class ResourceNotCoveredError(RuntimeError):
    """Inspector is on, but this particular resource is not being scanned."""


class InspectorFindingsCollector(SecurityFindingsCollector):
    """Collects active Inspector findings for one Lambda function.

    Findings describe the **currently deployed** version, not the candidate.
    See `SecurityFindings.is_candidate_artifact` for why that distinction
    matters and what it costs.
    """

    def __init__(
        self,
        function_name: str,
        client: Any = None,
        max_findings: int = MAX_FINDINGS,
    ) -> None:
        self._function_name = function_name
        self._client = client
        self._max_findings = max_findings

    @property
    def client(self) -> Any:
        # Imported and constructed lazily so the module stays importable — and
        # unit-testable — with no boto3 session and no credentials.
        if self._client is None:
            import boto3
            from botocore.config import Config

            self._client = boto3.client(
                "inspector2",
                config=Config(
                    retries={"max_attempts": 2, "mode": "standard"},
                    connect_timeout=3,
                    read_timeout=int(self.timeout_seconds),
                ),
            )
        return self._client

    # --- Step 1: is anybody looking at all? -------------------------------

    def _assert_lambda_scanning_enabled(self) -> None:
        """Verify Inspector is enabled AND Lambda standard scanning is on.

        Two separate switches. Inspector can be ENABLED for the account while
        Lambda scanning specifically is DISABLED, in which case ListFindings
        still returns an empty list for a Lambda and still means nothing.
        """
        resp = self.client.batch_get_account_status()
        accounts = resp.get("accounts", [])
        if not accounts:
            raise InspectorNotEnabledError(
                "BatchGetAccountStatus returned no accounts; cannot confirm Inspector is enabled"
            )

        account = accounts[0]
        overall = account.get("state", {}).get("status", "UNKNOWN")
        resource_state = account.get("resourceState", {})
        lambda_status = resource_state.get("lambda", {}).get("status", "UNKNOWN")

        if overall != "ENABLED":
            raise InspectorNotEnabledError(
                f"Inspector is {overall} for this account; an empty findings list "
                "would mean 'never scanned', not 'no vulnerabilities'"
            )
        if lambda_status != "ENABLED":
            raise InspectorNotEnabledError(
                f"Inspector Lambda standard scanning is {lambda_status}; "
                "Lambda findings cannot exist regardless of account status"
            )

    # --- Step 2: is anybody looking at THIS function? ---------------------

    def _assert_function_covered(self) -> str:
        """Verify this specific function is covered, and return its scan status.

        Account-level enablement is not enough. A function added minutes ago may
        not have been scanned yet, and its findings list would be empty for a
        reason that has nothing to do with its security.
        """
        resp = self.client.list_coverage(
            filterCriteria={
                "resourceType": [{"comparison": "EQUALS", "value": "AWS_LAMBDA_FUNCTION"}],
                "lambdaFunctionName": [{"comparison": "EQUALS", "value": self._function_name}],
            }
        )
        covered = resp.get("coveredResources", [])
        if not covered:
            raise ResourceNotCoveredError(
                f"{self._function_name} does not appear in Inspector coverage; "
                "it is not being scanned"
            )

        statuses = {
            r.get("scanStatus", {}).get("statusCode", "UNKNOWN")
            for r in covered
            if r.get("scanType") in (None, "PACKAGE", "PACKAGE_VULNERABILITY")
        } or {r.get("scanStatus", {}).get("statusCode", "UNKNOWN") for r in covered}

        if not statuses & HEALTHY_SCAN_STATUSES:
            reasons = {r.get("scanStatus", {}).get("reason", "") for r in covered}
            raise ResourceNotCoveredError(
                f"{self._function_name} is covered but not actively scanned "
                f"(status={sorted(statuses)}, reason={sorted(r for r in reasons if r)})"
            )
        return "ACTIVE"

    # --- Step 3: only now, the findings ----------------------------------

    def _list_active_findings(self) -> list[dict[str, Any]]:
        """Page through ACTIVE findings for this function.

        `findingStatus: ACTIVE` is not optional. Without it the list includes
        SUPPRESSED findings somebody deliberately accepted and CLOSED ones
        already remediated — reporting either as current would make the gate
        block deploys over vulnerabilities that no longer exist.
        """
        criteria = {
            "resourceType": [{"comparison": "EQUALS", "value": "AWS_LAMBDA_FUNCTION"}],
            "lambdaFunctionName": [{"comparison": "EQUALS", "value": self._function_name}],
            "findingStatus": [{"comparison": "EQUALS", "value": "ACTIVE"}],
        }

        findings: list[dict[str, Any]] = []
        next_token: str | None = None
        pages = 0
        while True:
            kwargs: dict[str, Any] = {"filterCriteria": criteria, "maxResults": 100}
            if next_token:
                kwargs["nextToken"] = next_token
            resp = self.client.list_findings(**kwargs)
            findings.extend(resp.get("findings", []))
            next_token = resp.get("nextToken")
            pages += 1
            # Hard stop on pagination. A runaway loop inside a Lambda burns the
            # timeout and produces no signal at all, which is worse than a
            # capped one that admits the cap.
            if not next_token or pages >= 10 or len(findings) > self._max_findings * 4:
                break
        return findings

    def _collect(self) -> SecurityFindings:
        self._assert_lambda_scanning_enabled()
        self._assert_function_covered()

        raw = self._list_active_findings()
        parsed = [normalise_finding(f) for f in raw]
        parsed = [f for f in parsed if f is not None]

        # Sort worst-first so that truncation drops the least important.
        order = {
            Severity.CRITICAL: 0,
            Severity.HIGH: 1,
            Severity.UNKNOWN: 2,
            Severity.MEDIUM: 3,
            Severity.LOW: 4,
            Severity.INFORMATIONAL: 5,
        }
        parsed.sort(key=lambda f: order.get(f.severity, 9))

        result = SecurityFindings(
            findings=tuple(parsed[: self._max_findings]),
            scanned_at=datetime.now(UTC),
            scanner="amazon-inspector2",
            scan_target=self._function_name,
            # Inspector scanned the deployed function, not the artifact waiting
            # in the pipeline. The verdict layer needs to know which.
            is_candidate_artifact=False,
        )

        if len(parsed) > self._max_findings:
            raise PartialSignal(
                result,
                f"{len(parsed)} findings found, carrying the {self._max_findings} "
                "most severe; counts below are therefore a floor, not a total",
            )
        return result


def normalise_finding(raw: dict[str, Any]) -> SecurityFinding | None:
    """Convert one Inspector finding into our shape. Returns None if unusable.

    Returning None rather than raising is deliberate: one malformed finding in a
    list of forty should not discard the other thirty-nine. It is logged, so the
    loss is visible rather than silent.
    """
    try:
        severity = SEVERITY_MAP.get(str(raw.get("severity", "")).upper(), Severity.UNKNOWN)
        details = raw.get("packageVulnerabilityDetails", {}) or {}
        vuln_id = details.get("vulnerabilityId") or raw.get("findingArn", "")

        packages = details.get("vulnerablePackages") or []
        first = packages[0] if packages else {}

        return SecurityFinding(
            id=str(vuln_id)[:120],
            severity=severity,
            title=str(raw.get("title", ""))[:200],
            package=first.get("name"),
            installed_version=first.get("version"),
            # `fixedInVersion` of "NotAvailable" is Inspector's way of saying
            # there is no patch. Treated as absent so `is_fixable` stays
            # truthful -- a literal "NotAvailable" string would read as a
            # version number and make an unfixable finding look fixable.
            fixed_version=_clean_fix_version(first.get("fixedInVersion")),
        )
    except (KeyError, TypeError, ValueError, IndexError):
        logger.warning("could not normalise Inspector finding: %s", raw.get("findingArn"))
        return None


def _clean_fix_version(value: Any) -> str | None:
    if not value:
        return None
    text = str(value).strip()
    if not text or text.lower() in {"notavailable", "none", "n/a"}:
        return None
    return text
