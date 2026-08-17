"""Tests for the Amazon Inspector collector. Phase 2.3.

The first group is the one that matters. It asserts that an empty findings list
from an unscanned resource is reported as UNAVAILABLE and never as "no
vulnerabilities" -- which is what the real API returned from this account while
Inspector was switched off.

A fake client is used rather than moto because the behaviour under test is our
guard sequence, not boto3's serialisation. The fake records which calls were made
and in what order, which is exactly what needs asserting: that findings are never
requested before enablement and coverage have been established.
"""

from __future__ import annotations

import pytest
from signals import (
    InspectorFindingsCollector,
    Severity,
    SignalStatus,
    normalise_finding,
)

FUNCTION = "ai-pre-traffic-gate-demo-app"


class FakeInspector:
    """Records calls, returns scripted responses."""

    def __init__(
        self,
        account_status: str = "ENABLED",
        lambda_status: str = "ENABLED",
        coverage: list | None = None,
        findings: list | None = None,
        pages: list | None = None,
    ):
        self.calls: list[str] = []
        self._account_status = account_status
        self._lambda_status = lambda_status
        self._coverage = (
            coverage
            if coverage is not None
            else [{"resourceId": FUNCTION, "scanStatus": {"statusCode": "ACTIVE"}}]
        )
        self._findings = findings if findings is not None else []
        self._pages = pages
        self._page_index = 0

    def batch_get_account_status(self, **_):
        self.calls.append("batch_get_account_status")
        return {
            "accounts": [
                {
                    "accountId": "594380318102",
                    "state": {"status": self._account_status},
                    "resourceState": {"lambda": {"status": self._lambda_status}},
                }
            ]
        }

    def list_coverage(self, **_):
        self.calls.append("list_coverage")
        return {"coveredResources": self._coverage}

    def list_findings(self, **kwargs):
        self.calls.append("list_findings")
        self.last_criteria = kwargs.get("filterCriteria", {})
        if self._pages is not None:
            page = self._pages[self._page_index]
            self._page_index += 1
            return page
        return {"findings": self._findings}


def finding(severity="CRITICAL", vuln_id="CVE-2024-1", fixed="1.2.1", name="example-lib"):
    return {
        "findingArn": f"arn:aws:inspector2:us-east-1:594380318102:finding/{vuln_id}",
        "severity": severity,
        "title": f"{vuln_id} - {name}",
        "type": "PACKAGE_VULNERABILITY",
        "packageVulnerabilityDetails": {
            "vulnerabilityId": vuln_id,
            "vulnerablePackages": [
                {"name": name, "version": "1.2.0", "fixedInVersion": fixed},
            ],
        },
    }


# --- THE point of this collector ------------------------------------------


def test_disabled_inspector_is_unavailable_not_zero_findings():
    """The real failure this collector exists to prevent.

    `list-findings` against this account returned `{"findings": []}` while
    Inspector had never been enabled. Three lines of naive code would report a
    clean bill of health for a service nobody has ever scanned.
    """
    fake = FakeInspector(account_status="DISABLED", lambda_status="DISABLED")

    result = InspectorFindingsCollector(FUNCTION, client=fake).collect()

    assert result.status is SignalStatus.UNAVAILABLE
    assert result.data is None
    assert "DISABLED" in result.error
    # And critically: it never even asked for findings.
    assert "list_findings" not in fake.calls


def test_lambda_scanning_off_while_inspector_on_is_still_unavailable():
    """Two separate switches. Account ENABLED is not enough."""
    fake = FakeInspector(account_status="ENABLED", lambda_status="DISABLED")

    result = InspectorFindingsCollector(FUNCTION, client=fake).collect()

    assert result.status is SignalStatus.UNAVAILABLE
    assert "Lambda standard scanning is DISABLED" in result.error
    assert "list_findings" not in fake.calls


def test_uncovered_function_is_unavailable_not_zero_findings():
    """Inspector on, this function not scanned. Empty list still means nothing."""
    fake = FakeInspector(coverage=[])

    result = InspectorFindingsCollector(FUNCTION, client=fake).collect()

    assert result.status is SignalStatus.UNAVAILABLE
    assert "not being scanned" in result.error
    assert "list_findings" not in fake.calls


def test_covered_but_inactive_scan_is_unavailable():
    """A function awaiting its first scan has an empty list for a reason."""
    fake = FakeInspector(
        coverage=[
            {
                "resourceId": FUNCTION,
                "scanStatus": {"statusCode": "INACTIVE", "reason": "SCAN_IN_PROGRESS"},
            }
        ]
    )

    result = InspectorFindingsCollector(FUNCTION, client=fake).collect()

    assert result.status is SignalStatus.UNAVAILABLE
    assert "not actively scanned" in result.error


def test_the_guard_runs_in_order_before_any_findings_request():
    """Enablement, then coverage, then findings. Never out of order."""
    fake = FakeInspector()

    InspectorFindingsCollector(FUNCTION, client=fake).collect()

    assert fake.calls == ["batch_get_account_status", "list_coverage", "list_findings"]


def test_a_genuinely_clean_scan_reports_zero_findings():
    """The other half. Once the guard passes, empty DOES mean clean."""
    fake = FakeInspector(findings=[])

    result = InspectorFindingsCollector(FUNCTION, client=fake).collect()

    assert result.status is SignalStatus.OK
    assert result.data.findings == ()
    assert result.data.critical_count == 0
    assert result.data.scanner == "amazon-inspector2"


# --- What was actually scanned --------------------------------------------


def test_findings_are_labelled_as_describing_the_deployed_version():
    """Inspector scans what is running, not what is about to run."""
    result = InspectorFindingsCollector(FUNCTION, client=FakeInspector()).collect()

    assert result.data.is_candidate_artifact is False
    assert result.data.scan_target == FUNCTION


# --- Only active findings -------------------------------------------------


def test_only_active_findings_are_requested():
    """Suppressed and closed findings must not be reported as current.

    Blocking a deploy over a vulnerability somebody deliberately accepted, or one
    already remediated, is how a gate loses credibility.
    """
    fake = FakeInspector()

    InspectorFindingsCollector(FUNCTION, client=fake).collect()

    statuses = fake.last_criteria.get("findingStatus", [])
    assert statuses == [{"comparison": "EQUALS", "value": "ACTIVE"}]


# --- Normalisation --------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("CRITICAL", Severity.CRITICAL),
        ("HIGH", Severity.HIGH),
        ("MEDIUM", Severity.MEDIUM),
        ("LOW", Severity.LOW),
        ("INFORMATIONAL", Severity.INFORMATIONAL),
        ("UNTRIAGED", Severity.UNKNOWN),
        ("SOMETHING_AWS_ADDED_LATER", Severity.UNKNOWN),
        ("", Severity.UNKNOWN),
    ],
)
def test_severity_mapping_never_silently_downgrades(raw, expected):
    """An unscored or unrecognised severity must not become LOW."""
    assert normalise_finding(finding(severity=raw)).severity is expected


def test_not_available_fix_version_is_treated_as_no_fix():
    """Inspector says "NotAvailable" where there is no patch.

    Kept as a literal string it would read as a version number, and
    `is_fixable` would report True for something with no remedy at all.
    """
    parsed = normalise_finding(finding(fixed="NotAvailable"))

    assert parsed.fixed_version is None
    assert parsed.is_fixable is False


def test_a_real_fix_version_is_kept():
    parsed = normalise_finding(finding(fixed="1.2.1"))

    assert parsed.fixed_version == "1.2.1"
    assert parsed.is_fixable is True


def test_a_malformed_finding_is_dropped_not_fatal():
    """One bad finding must not discard the rest of the list."""
    assert normalise_finding({"severity": "CRITICAL"}) is not None  # sparse but usable

    fake = FakeInspector(findings=[finding(), {"severity": "HIGH"}, finding(vuln_id="CVE-2024-2")])
    result = InspectorFindingsCollector(FUNCTION, client=fake).collect()

    assert result.status is SignalStatus.OK
    assert len(result.data.findings) == 3


def test_findings_are_sorted_worst_first():
    fake = FakeInspector(
        findings=[
            finding(severity="LOW", vuln_id="CVE-L"),
            finding(severity="CRITICAL", vuln_id="CVE-C"),
            finding(severity="MEDIUM", vuln_id="CVE-M"),
            finding(severity="HIGH", vuln_id="CVE-H"),
        ]
    )

    result = InspectorFindingsCollector(FUNCTION, client=fake).collect()

    assert [f.severity for f in result.data.findings] == [
        Severity.CRITICAL,
        Severity.HIGH,
        Severity.MEDIUM,
        Severity.LOW,
    ]


# --- Truncation is DEGRADED, not silent ------------------------------------


def test_too_many_findings_produces_a_degraded_signal():
    """Real data with an admitted limit, rather than a silently short list."""
    many = [finding(vuln_id=f"CVE-{i}", severity="HIGH") for i in range(30)]
    fake = FakeInspector(findings=many)

    result = InspectorFindingsCollector(FUNCTION, client=fake, max_findings=10).collect()

    assert result.status is SignalStatus.DEGRADED
    # Degraded still carries data -- throwing it away would be as dishonest as
    # pretending the list was complete.
    assert result.data is not None
    assert len(result.data.findings) == 10
    assert result.is_usable
    assert "30 findings found" in result.error
    assert "floor, not a total" in result.error


def test_truncation_keeps_the_worst_findings():
    fake = FakeInspector(
        findings=[finding(severity="LOW", vuln_id=f"CVE-L{i}") for i in range(20)]
        + [finding(severity="CRITICAL", vuln_id="CVE-CRIT")]
    )

    result = InspectorFindingsCollector(FUNCTION, client=fake, max_findings=3).collect()

    assert result.data.findings[0].severity is Severity.CRITICAL


def test_pagination_is_followed():
    pages = [
        {"findings": [finding(vuln_id="CVE-1")], "nextToken": "t1"},
        {"findings": [finding(vuln_id="CVE-2")]},
    ]
    fake = FakeInspector(pages=pages)

    result = InspectorFindingsCollector(FUNCTION, client=fake).collect()

    assert fake.calls.count("list_findings") == 2
    assert len(result.data.findings) == 2


# --- API failures fail closed ---------------------------------------------


def test_an_api_error_is_unavailable():
    class Broken(FakeInspector):
        def batch_get_account_status(self, **_):
            raise RuntimeError("AccessDeniedException")

    result = InspectorFindingsCollector(FUNCTION, client=Broken()).collect()

    assert result.status is SignalStatus.UNAVAILABLE
    assert result.data is None
    assert "AccessDeniedException" in result.error


def test_an_empty_account_list_is_unavailable():
    class NoAccounts(FakeInspector):
        def batch_get_account_status(self, **_):
            return {"accounts": []}

    result = InspectorFindingsCollector(FUNCTION, client=NoAccounts()).collect()

    assert result.status is SignalStatus.UNAVAILABLE
    assert "cannot confirm" in result.error
