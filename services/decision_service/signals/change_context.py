"""The real change-context collector. Phase 2.2.

Reads what is being deployed out of the CodePipeline job event the gate is
invoked with. No git, no GitHub API, no artifact download -- the facts arrive as
`UserParameters` on the Lambda invoke action, assembled by CodePipeline from two
places with very different trust properties.

WHY IT ARRIVES BASE64-ENCODED, which looks like unnecessary ceremony until you
try the obvious thing:

`UserParameters` is a JSON string that CodePipeline builds by literal text
substitution of `#{SourceVariables.CommitMessage}` and friends. There is no
escaping step. So the natural implementation --

    {"commit_message": "#{SourceVariables.CommitMessage}", ...}

-- breaks the moment somebody writes a commit message containing a double
quote. Not maliciously; `fix the "off by one" in retry` is enough. The gate then
fails to parse its own configuration.

That is worth sitting with, because it is the whole project in miniature:
attacker-influenced text reaches your own config format *before* it reaches the
model, and the first thing it breaks is structural, not semantic. Encoding the
payload means the only characters CodePipeline ever substitutes are base64
alphabet and hex, neither of which can terminate a JSON string.

THE CROSS-CHECK, which is the part worth explaining on stage:

Diff statistics are computed by `buildspec.yml`, which lives in the repository
being judged. A change can therefore rewrite the code that measures it. Nothing
here can prevent that -- but the commit SHA arrives twice, by two independent
routes:

    trusted_commit_sha    from CodePipeline, read off the source connection
    commit_sha            from inside the build's own encoded payload

If they disagree, the build has described a commit other than the one
CodePipeline sourced. That is not a parsing problem, it is a tampering
indicator, and it fails closed. It does not catch a build that lies about line
counts for the *correct* commit -- provenance labelling handles that by making
the claim visible rather than by verifying it -- but it does catch a build
reporting on an entirely different change.
"""

from __future__ import annotations

import base64
import binascii
import json
from datetime import datetime
from typing import Any

from .collectors import ChangeContextCollector
from .types import ChangeContext, Provenance

# CodePipeline caps UserParameters at 1000 characters. The build trims its
# payload to fit; this is the last line of defence and exists so that a
# truncated blob fails loudly at decode time rather than silently losing the
# tail of a JSON document.
MAX_USER_PARAMETERS = 1000


class PipelineEventError(ValueError):
    """The job event was not shaped the way CodePipeline documents."""


def extract_user_parameters(event: dict[str, Any]) -> dict[str, Any]:
    """Pull the UserParameters JSON out of a CodePipeline job event.

    Kept separate from the collector so it can be tested against realistic
    event shapes without constructing a collector, and so the failure modes are
    individually nameable rather than one broad "bad event".
    """
    try:
        job = event["CodePipeline.job"]
        raw = job["data"]["actionConfiguration"]["configuration"]["UserParameters"]
    except (KeyError, TypeError) as exc:
        raise PipelineEventError(f"not a CodePipeline job event: missing {exc}") from exc

    if len(raw) > MAX_USER_PARAMETERS:
        raise PipelineEventError(
            f"UserParameters is {len(raw)} chars, over the {MAX_USER_PARAMETERS} limit; "
            "it has probably been truncated and cannot be trusted"
        )

    try:
        params = json.loads(raw)
    except json.JSONDecodeError as exc:
        # Most likely cause by a wide margin: an unencoded field carrying a
        # quote or newline. Say so, because the raw error alone sends people
        # looking for a bug in the pipeline definition.
        raise PipelineEventError(
            f"UserParameters is not valid JSON ({exc}); "
            "check that every interpolated value is base64 or hex"
        ) from exc

    if not isinstance(params, dict):
        raise PipelineEventError(
            f"UserParameters decoded to {type(params).__name__}, not an object"
        )

    return params


def decode_payload(encoded: str) -> dict[str, Any]:
    """Decode the base64 change-context blob the build produced."""
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise PipelineEventError(f"change context is not valid base64: {exc}") from exc

    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise PipelineEventError(f"decoded change context is not valid JSON: {exc}") from exc

    if not isinstance(payload, dict):
        raise PipelineEventError(
            f"decoded change context is {type(payload).__name__}, not an object"
        )

    return payload


class PipelineChangeContextCollector(ChangeContextCollector):
    """Builds a ChangeContext from a CodePipeline Lambda-invoke job event.

    Raises on anything it cannot vouch for. The base class turns that into an
    UNAVAILABLE result with no data, and `SignalBundle.has_required_signals`
    then reports False -- which routes the deploy to human review rather than
    to a model asked to judge a change it was never told about.
    """

    def __init__(self, event: dict[str, Any]):
        self._event = event

    def _collect(self) -> ChangeContext:
        params = extract_user_parameters(self._event)

        trusted_sha = params.get("trusted_commit_sha", "")
        encoded = params.get("change_context_b64", "")
        if not encoded:
            raise PipelineEventError("UserParameters carried no change_context_b64")

        payload = decode_payload(encoded)

        # --- the cross-check -------------------------------------------------
        reported_sha = str(payload.get("commit_sha", ""))
        if not trusted_sha:
            raise PipelineEventError(
                "no trusted_commit_sha from CodePipeline; the build's account of "
                "itself cannot be corroborated"
            )
        if not reported_sha:
            raise PipelineEventError("build payload did not state which commit it describes")
        # Compared on the shorter length because CodePipeline supplies the full
        # 40-character SHA while a build may abbreviate. A prefix match is the
        # correct comparison for git SHAs and is not a weakening.
        n = min(len(trusted_sha), len(reported_sha))
        if n < 7 or trusted_sha[:n].lower() != reported_sha[:n].lower():
            raise PipelineEventError(
                f"commit SHA mismatch: CodePipeline sourced {trusted_sha[:12]!r} but the "
                f"build described {reported_sha[:12]!r}; refusing to judge a change on "
                "another change's measurements"
            )

        committed_at = _parse_timestamp(payload.get("committed_at"))

        # Diff statistics are optional. A build that could not compute them --
        # a shallow clone, a first commit with no parent -- says so, and the
        # result is a DEGRADED signal rather than a fabricated zero. Zero files
        # changed is a meaningful claim and must not be the value that means
        # "we could not tell".
        has_diff = bool(payload.get("diff_stats_ok"))
        if not has_diff:
            raise PipelineEventError(
                "build reported that it could not compute diff statistics; "
                "change context is incomplete"
            )

        # Deploy cadence is genuinely absent here rather than zero. The build
        # cannot see deployment history -- that is in the CodeDeploy control
        # plane, which a build has no business reading. A dedicated collector
        # with AWS_API provenance is the right home for it; until then the field
        # is honestly empty.
        raw_cadence = payload.get("deploys_last_24h")
        cadence = None if raw_cadence is None else _as_int(raw_cadence)

        return ChangeContext(
            commit_sha=trusted_sha,
            commit_message=str(payload.get("commit_message", "")),
            branch=str(payload.get("branch", "")),
            author=str(payload.get("author", "")),
            committed_at=committed_at,
            files_changed=_as_int(payload.get("files_changed")),
            lines_added=_as_int(payload.get("lines_added")),
            lines_removed=_as_int(payload.get("lines_removed")),
            paths=tuple(str(p) for p in payload.get("paths", ())),
            deploys_last_24h=cadence,
            # The SHA, branch and author reach us via CodePipeline's own read of
            # the source connection. The diff numbers were produced by a script
            # inside the repository. Same object, two trust levels, recorded.
            metadata_provenance=Provenance.PIPELINE,
            diff_provenance=Provenance.BUILD,
            cadence_provenance=(Provenance.NONE if cadence is None else Provenance.BUILD),
        )


def _parse_timestamp(value: Any) -> datetime:
    if not value:
        raise PipelineEventError("change context has no commit timestamp")
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError as exc:
        raise PipelineEventError(f"unparseable commit timestamp {value!r}: {exc}") from exc
    if parsed.tzinfo is None:
        # A naive timestamp would make `is_off_hours` silently wrong rather than
        # absent, and an off-hours flag is one of the signals most likely to
        # move a verdict. Refuse it.
        raise PipelineEventError(f"commit timestamp {value!r} has no timezone")
    return parsed


def _as_int(value: Any) -> int:
    if value is None:
        raise PipelineEventError("expected an integer in the change context, found nothing")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise PipelineEventError(f"expected an integer, found {value!r}") from exc
