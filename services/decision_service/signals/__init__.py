"""Signal collection for the deployment gate. Phase 2.

The gate's job is to judge a deploy. This package is everything it knows before
it judges, and -- just as importantly -- an explicit account of what it failed
to learn.

    types.py       what a signal looks like once normalised
    base.py        the collector interface and the fail-closed guarantee
    collectors.py  per-domain base classes and their mock implementations
    bundle.py      assembling three signals into one auditable record
    scenarios.py   fixed situations for tests, eval fixtures, and the demo

Start with `base.py`. The single idea holding the package together is that an
absent signal must never be mistaken for a reassuring one.
"""

from .base import (
    DisabledCollector,
    PartialSignal,
    SignalCollector,
    SignalResult,
    SignalStatus,
)
from .bundle import REQUIRED_SIGNALS, SignalBundle, collect_signals
from .change_context import (
    PipelineChangeContextCollector,
    PipelineEventError,
    decode_payload,
    extract_user_parameters,
)
from .cloudwatch import MetricsUnavailableError, TargetHealthCloudWatchCollector
from .collectors import (
    ChangeContextCollector,
    MockChangeContextCollector,
    MockSecurityFindingsCollector,
    MockTargetHealthCollector,
    SecurityFindingsCollector,
    TargetHealthCollector,
)
from .deploy_cadence import DeployCadence, DeployCadenceCollector
from .inspector import (
    InspectorFindingsCollector,
    InspectorNotEnabledError,
    ResourceNotCoveredError,
    normalise_finding,
)
from .types import (
    Alarm,
    ChangeContext,
    DeploymentTarget,
    Provenance,
    SecurityFinding,
    SecurityFindings,
    Severity,
    TargetHealth,
)

__all__ = [
    "REQUIRED_SIGNALS",
    "Alarm",
    "ChangeContext",
    "ChangeContextCollector",
    "DeployCadence",
    "DeployCadenceCollector",
    "DeploymentTarget",
    "DisabledCollector",
    "MetricsUnavailableError",
    "MockChangeContextCollector",
    "InspectorFindingsCollector",
    "InspectorNotEnabledError",
    "MockSecurityFindingsCollector",
    "MockTargetHealthCollector",
    "PartialSignal",
    "PipelineChangeContextCollector",
    "PipelineEventError",
    "Provenance",
    "ResourceNotCoveredError",
    "SecurityFinding",
    "SecurityFindings",
    "SecurityFindingsCollector",
    "Severity",
    "SignalBundle",
    "SignalCollector",
    "SignalResult",
    "SignalStatus",
    "TargetHealth",
    "TargetHealthCloudWatchCollector",
    "TargetHealthCollector",
    "collect_signals",
    "decode_payload",
    "normalise_finding",
    "extract_user_parameters",
]
