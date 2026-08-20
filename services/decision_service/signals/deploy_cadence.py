"""Deploy cadence from the AWS CodeDeploy control plane. Phase 2.4b.

The last field in the signal bundle that was honestly empty.

Cadence answers "how often has this service been deployed lately", and it moves
a verdict in both directions:

  * Seven deploys in one day usually means somebody is chasing a problem. The
    eighth is riskier than the first even when the diff is three lines -- and the
    change itself carries no trace of that, which is precisely the kind of
    context a test suite cannot supply.
  * Three weeks since the last deploy means a large accumulated delta and cold
    operational reflexes. Also riskier, for opposite reasons.

WHY THIS IS NOT COMPUTED IN THE BUILD, unlike the diff statistics:

Deployment history lives in the CodeDeploy control plane. `buildspec.yml` cannot
see it, and giving the build CodeDeploy permissions to look would hand a
repository file the ability to read -- and eventually influence -- deployment
state. So this is read by the gate itself, directly from the API, and carries
`Provenance.AWS_API`.

That makes it the first signal in the bundle that the change being judged has no
way to lie about. Diff statistics are self-reported by a script the change can
edit (see `change_context.py`); cadence is not. Worth noting on a slide: the
provenance labels are not decoration, they describe genuinely different threat
surfaces.

WHAT THIS COLLECTOR MUST NOT DO: create a deployment. It reads
`ListDeployments` and `BatchGetDeployments` and nothing else. The gate has no
`codedeploy:CreateDeployment`, which stays with the executor (D-016). Read and
write on the same service are separable, and this is where that separation earns
its keep.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from .base import SignalCollector

logger = logging.getLogger(__name__)

DEFAULT_WINDOW_HOURS = 24

# How far back to look for the most recent deployment when computing the gap.
# Beyond this the answer is "longer ago than we bothered to look", which is
# reported as None rather than as a very large number we cannot substantiate.
LOOKBACK_DAYS = 90

# Deployment statuses that count as a deploy having happened. A deployment that
# failed or was stopped still tells you somebody was trying to change this
# service, which is the signal cadence is actually measuring -- so they count.
# Only deployments that never started are excluded.
COUNTED_STATUSES = ("Created", "Queued", "InProgress", "Baking", "Succeeded", "Failed", "Stopped")


@dataclass(frozen=True)
class DeployCadence:
    """How often this service has been deployed recently."""

    deploys_in_window: int
    hours_since_last_deploy: float | None
    window_hours: int = DEFAULT_WINDOW_HOURS

    @property
    def is_rapid_succession(self) -> bool:
        """Several deploys in quick succession: usually an unfolding incident.

        Computed here rather than left for the model to infer from two numbers.
        Anything a deterministic function can decide should not be delegated to a
        probabilistic one, and Phase 4 can assert on it.
        """
        return self.deploys_in_window >= 5

    @property
    def is_first_in_a_long_time(self) -> bool:
        return self.hours_since_last_deploy is not None and self.hours_since_last_deploy >= 168


class DeployCadenceCollector(SignalCollector[DeployCadence]):
    """Reads recent deployment history for one CodeDeploy deployment group."""

    name = "deploy_cadence"

    def __init__(
        self,
        application_name: str,
        deployment_group_name: str,
        client: Any = None,
        window_hours: int = DEFAULT_WINDOW_HOURS,
        now: datetime | None = None,
    ) -> None:
        self._application = application_name
        self._group = deployment_group_name
        self._client = client
        self._window_hours = window_hours
        self._now = now

    @property
    def client(self) -> Any:
        if self._client is None:
            import boto3
            from botocore.config import Config

            self._client = boto3.client(
                "codedeploy",
                config=Config(
                    retries={"max_attempts": 2, "mode": "standard"},
                    connect_timeout=3,
                    read_timeout=int(self.timeout_seconds),
                ),
            )
        return self._client

    def _list_ids(self, start: datetime, end: datetime) -> list[str]:
        """Deployment IDs created in a window, most recent first."""
        ids: list[str] = []
        next_token: str | None = None
        pages = 0
        while True:
            kwargs: dict[str, Any] = {
                "applicationName": self._application,
                "deploymentGroupName": self._group,
                "includeOnlyStatuses": list(COUNTED_STATUSES),
                "createTimeRange": {"start": start, "end": end},
            }
            if next_token:
                kwargs["nextToken"] = next_token
            resp = self.client.list_deployments(**kwargs)
            ids.extend(resp.get("deployments", []))
            next_token = resp.get("nextToken")
            pages += 1
            # Bounded: a runaway loop inside a Lambda burns the timeout and
            # yields no signal at all, which is worse than a bounded count.
            if not next_token or pages >= 5:
                break
        return ids

    def _created_at(self, deployment_id: str) -> datetime | None:
        resp = self.client.batch_get_deployments(deploymentIds=[deployment_id])
        for info in resp.get("deploymentsInfo", []) or []:
            created = info.get("createTime")
            if isinstance(created, datetime):
                return created if created.tzinfo else created.replace(tzinfo=UTC)
        return None

    def _collect(self) -> DeployCadence:
        now = self._now or datetime.now(UTC)

        window_start = now - timedelta(hours=self._window_hours)
        in_window = self._list_ids(window_start, now)

        # The gate runs BEFORE the Deploy stage, so the deployment for this
        # pipeline execution does not exist yet. No off-by-one to correct -- but
        # worth stating, because it silently becomes wrong if the gate is ever
        # moved after the deploy.
        count = len(in_window)

        # The gap to the previous deploy needs a wider search than the count
        # window: a service last deployed three weeks ago has zero deploys in 24
        # hours and a very meaningful gap.
        recent = in_window or self._list_ids(now - timedelta(days=LOOKBACK_DAYS), now)

        hours_since: float | None = None
        if recent:
            created = self._created_at(recent[0])
            if created is not None:
                hours_since = max(0.0, (now - created).total_seconds() / 3600.0)

        return DeployCadence(
            deploys_in_window=count,
            hours_since_last_deploy=hours_since,
            window_hours=self._window_hours,
        )
