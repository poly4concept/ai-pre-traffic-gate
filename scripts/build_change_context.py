#!/usr/bin/env python3
"""Compute change context during the build and emit it base64-encoded.

Phase 2.2, build side. Runs inside CodeBuild, prints one base64 line on stdout,
which `buildspec.yml` captures into the `CHANGE_CONTEXT_B64` exported variable.
CodePipeline then interpolates that into the Gate action's `UserParameters`, and
`signals.change_context` decodes it.

Why a script rather than shell in the buildspec: producing valid JSON from bash
means quoting commit messages correctly, and getting that wrong is precisely the
class of bug this whole payload design exists to avoid. It is also testable
offline, which shell in a YAML string is not.

THREE THINGS THIS HAS TO SURVIVE, none of them hypothetical:

  1. No git repository at all. CodePipeline's default source artifact format is
     CODE_ZIP -- CodeBuild receives a zip with no `.git` directory, so every git
     command fails. Only `CODEBUILD_CLONE_REF` gives a real clone.
  2. A shallow clone. Even with a clone, history may be one commit deep, so
     `HEAD~1` does not exist and there is nothing to diff against.
  3. A root commit, which genuinely has no parent.

In all three the script emits a payload with `diff_stats_ok: false` rather than
zeros, and the collector turns that into an UNAVAILABLE signal. A build that
cannot measure the change says so; it does not report a 5,000-line refactor as
zero files changed.

Usage:
    python scripts/build_change_context.py            # emit base64
    python scripts/build_change_context.py --pretty   # readable JSON, for humans
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
from typing import Any

# CodePipeline caps UserParameters at 1000 characters, and everything else in
# that JSON object comes out of this blob's budget.
#
#   {"trusted_commit_sha":"<40>","change_context_b64":"<N>",
#    "pipeline_execution_id":"<36>"}
#
# The fixed keys and braces cost 76, the commit SHA 40, and the execution ID 36
# -- 152 before a single byte of payload. Phase 5.4 added that third field
# (F-023), which is why this dropped from 860: the previous budget plus the new
# wrapper came to roughly 1012, and exceeding the cap makes CodePipeline reject
# the whole pipeline definition -- a confusing failure a long way from its cause.
#
# 780 leaves ~68 characters of headroom rather than sitting on the limit.
MAX_B64_LENGTH = 780

# Trim order matters: paths first (individually least informative), then the
# commit message, which carries intent and is worth keeping longest.
MAX_PATHS = 12
MAX_MESSAGE_CHARS = 240


def git(*args: str) -> str | None:
    """Run a git command. Returns None on any failure, including git's absence.

    Ruff's S603/S607 are suppressed here with reasons, rather than switched off
    for the file -- this project lints for security posture on purpose, so a
    silenced rule needs an argument.

    S603 (untrusted input to subprocess): every call site in this module passes
    hardcoded literal arguments. No repository content, environment variable or
    command-line input reaches this list. There is no shell involved either,
    since a list argument means no shell interpretation.

    S607 (partial executable path): resolving `git` via PATH is deliberate. The
    binary sits at different paths in CodeBuild's Amazon Linux image and on a
    developer laptop, and hardcoding either would break the other.
    """
    try:
        out = subprocess.run(  # noqa: S603
            ["git", *args],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None
    return out.stdout.strip()


def resolve_sha() -> str:
    """The commit being built.

    CODEBUILD_RESOLVED_SOURCE_VERSION is set by CodeBuild regardless of source
    format, so it works even with no `.git` present. Preferred over `git
    rev-parse` for exactly that reason.
    """
    return os.environ.get("CODEBUILD_RESOLVED_SOURCE_VERSION") or git("rev-parse", "HEAD") or ""


def diff_stats() -> dict[str, Any]:
    """Diff HEAD against its parent. Reports failure rather than guessing."""
    if git("rev-parse", "--git-dir") is None:
        return {"diff_stats_ok": False, "diff_note": "no git repository in the build"}

    if git("rev-parse", "--verify", "HEAD~1") is None:
        # Shallow clone or root commit. Distinguishable, and worth
        # distinguishing: one is a configuration problem we can fix, the other
        # is a fact about the repository that we cannot.
        shallow = git("rev-parse", "--is-shallow-repository")
        note = "shallow clone: no parent commit available" if shallow == "true" else "root commit"
        return {"diff_stats_ok": False, "diff_note": note}

    numstat = git("diff", "--numstat", "HEAD~1", "HEAD")
    if numstat is None:
        return {"diff_stats_ok": False, "diff_note": "git diff failed"}

    added = removed = 0
    paths: list[str] = []
    for line in numstat.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        a, r, path = parts
        # Binary files report "-" for both counts. Counted as a changed file but
        # contributing no lines, which is the honest reading.
        added += int(a) if a.isdigit() else 0
        removed += int(r) if r.isdigit() else 0
        paths.append(path)

    return {
        "diff_stats_ok": True,
        "files_changed": len(paths),
        "lines_added": added,
        "lines_removed": removed,
        "paths": paths,
    }


def build_payload() -> dict[str, Any]:
    sha = resolve_sha()

    # `git log -1 --format=%cI` gives a strict-ISO committer date WITH offset.
    # %cd or %ci would need parsing, and a timezone-less timestamp is rejected
    # downstream -- deliberately, since it would make the off-hours flag quietly
    # wrong rather than absent.
    committed_at = git("log", "-1", "--format=%cI")
    message = git("log", "-1", "--format=%s")
    author = git("log", "-1", "--format=%an")

    branch = (
        os.environ.get("CODEBUILD_WEBHOOK_HEAD_REF", "").removeprefix("refs/heads/")
        or git("rev-parse", "--abbrev-ref", "HEAD")
        or ""
    )

    payload: dict[str, Any] = {
        "commit_sha": sha,
        "commit_message": message or "",
        "branch": branch,
        "author": author or "",
        "committed_at": committed_at or "",
    }
    payload.update(diff_stats())
    return payload


def trim_to_budget(payload: dict[str, Any]) -> dict[str, Any]:
    """Shrink the payload until its base64 form fits UserParameters.

    Trims in increasing order of value: path list first, then the commit
    message. Records what it dropped -- a silently truncated signal is a lie
    about completeness, and the verdict layer should know it is looking at a
    partial file list rather than a short one.
    """
    payload = dict(payload)

    paths = payload.get("paths")
    if isinstance(paths, list) and len(paths) > MAX_PATHS:
        payload["paths_omitted"] = len(paths) - MAX_PATHS
        payload["paths"] = paths[:MAX_PATHS]

    message = payload.get("commit_message", "")
    if len(message) > MAX_MESSAGE_CHARS:
        payload["commit_message"] = message[:MAX_MESSAGE_CHARS]
        payload["message_truncated"] = True

    # Then shed paths one at a time until it fits. A loop rather than arithmetic
    # because base64 length depends on the JSON encoding of whatever unicode the
    # commit message happens to contain, and guessing that is not worth it.
    while len(encode(payload)) > MAX_B64_LENGTH:
        current = payload.get("paths") or []
        if current:
            payload["paths"] = current[:-1]
            payload["paths_omitted"] = payload.get("paths_omitted", 0) + 1
            continue

        message = payload.get("commit_message", "")
        if len(message) > 40:
            payload["commit_message"] = message[: len(message) // 2]
            payload["message_truncated"] = True
            continue

        # Nothing left worth shedding. Emit anyway: the collector's length check
        # will reject it loudly, which beats guessing at what else to discard.
        break

    return payload


def encode(payload: dict[str, Any]) -> str:
    # Compact separators and sorted keys: smaller, and deterministic for the
    # same input, which the Phase 4 replay story depends on.
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    return base64.b64encode(raw).decode()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pretty", action="store_true", help="Print readable JSON instead.")
    args = parser.parse_args()

    payload = trim_to_budget(build_payload())

    if args.pretty:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    encoded = encode(payload)
    print(encoded)

    # Diagnostics to stderr so stdout stays a single clean value for the
    # buildspec to capture.
    print(
        f"change context: {len(encoded)} b64 chars, diff_stats_ok={payload.get('diff_stats_ok')}",
        file=sys.stderr,
    )
    if not payload.get("diff_stats_ok"):
        print(f"  reason: {payload.get('diff_note')}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
