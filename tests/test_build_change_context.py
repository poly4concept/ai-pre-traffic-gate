"""Tests for the build-side change context producer. Phase 2.2.

The most valuable test here is the round trip. `build_change_context.py` runs in
CodeBuild and `signals.change_context` runs in the gate Lambda; they never share
a process and agree only by convention about the payload format. A drift between
them would not fail either component's own tests -- it would surface as a gate
that fails closed on every deploy, which reads as a gate bug rather than a
format mismatch.

Nothing here shells out to git. The git-dependent paths are exercised by running
the script for real, which was done during development and produced a valid
820-character payload from this repository's own history.
"""

from __future__ import annotations

import base64
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest
from signals import PipelineChangeContextCollector, SignalStatus


def load_script() -> ModuleType:
    path = Path(__file__).resolve().parents[1] / "scripts" / "build_change_context.py"
    spec = importlib.util.spec_from_file_location("_script_build_change_context", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["_script_build_change_context"] = module
    spec.loader.exec_module(module)
    return module


bcc = load_script()

SHA = "dcc3c4bc37e199ff636f0b7df0e610691ca3860c"


def base_payload(**overrides):
    payload = {
        "commit_sha": SHA,
        "commit_message": "Phase 1 increment 3: pipeline, executor, buildspec",
        "branch": "main",
        "author": "Poly4",
        "committed_at": "2026-08-12T12:57:39+01:00",
        "diff_stats_ok": True,
        "files_changed": 3,
        "lines_added": 40,
        "lines_removed": 2,
        "paths": ["a.py", "b.py", "c.py"],
    }
    payload.update(overrides)
    return payload


# --- The round trip -------------------------------------------------------


def test_the_build_payload_is_readable_by_the_gate_collector():
    """The contract between two processes that never meet."""
    encoded = bcc.encode(bcc.trim_to_budget(base_payload()))
    params = {"trusted_commit_sha": SHA, "change_context_b64": encoded}
    event = {
        "CodePipeline.job": {
            "id": "job-1",
            "data": {
                "actionConfiguration": {"configuration": {"UserParameters": json.dumps(params)}}
            },
        }
    }

    result = PipelineChangeContextCollector(event).collect()

    assert result.status is SignalStatus.OK, result.error
    assert result.data.commit_sha == SHA
    assert result.data.files_changed == 3
    assert result.data.lines_added == 40
    assert result.data.paths == ("a.py", "b.py", "c.py")


def test_the_wrapped_user_parameters_stay_under_the_hard_limit():
    """Budget check on the real thing CodePipeline sees, not just the payload.

    The collector rejects anything over 1000 characters as probably truncated,
    so the build's own budget has to leave room for the wrapper JSON and the
    40-character SHA alongside it.
    """
    payload = base_payload(
        commit_message="x" * 500,
        paths=[f"services/some/deeply/nested/module_{i}.py" for i in range(60)],
    )
    encoded = bcc.encode(bcc.trim_to_budget(payload))
    wrapped = json.dumps({"trusted_commit_sha": SHA, "change_context_b64": encoded})

    assert len(wrapped) < 1000, f"wrapped UserParameters would be {len(wrapped)} chars"


# --- Trimming -------------------------------------------------------------


def test_dropped_paths_are_counted_not_silently_lost():
    """A truncated list must not read as a short list."""
    payload = base_payload(paths=[f"file_{i}.py" for i in range(40)], files_changed=40)

    trimmed = bcc.trim_to_budget(payload)

    assert len(trimmed["paths"]) < 40
    assert trimmed["paths_omitted"] == 40 - len(trimmed["paths"])
    # The COUNT survives trimming even though the list does not. The verdict
    # layer still knows this was a 40-file change.
    assert trimmed["files_changed"] == 40


def test_long_commit_messages_are_marked_as_truncated():
    trimmed = bcc.trim_to_budget(base_payload(commit_message="y" * 900))

    assert trimmed["message_truncated"] is True
    assert len(trimmed["commit_message"]) < 900


def test_a_short_payload_is_left_alone():
    payload = base_payload()

    trimmed = bcc.trim_to_budget(payload)

    assert trimmed["paths"] == payload["paths"]
    assert "paths_omitted" not in trimmed
    assert "message_truncated" not in trimmed


def test_paths_are_shed_before_the_commit_message():
    """Intent is worth more than a file list, so the message survives longer."""
    payload = base_payload(
        commit_message="a meaningful description of intent " * 4,
        paths=[f"services/module_{i}.py" for i in range(50)],
    )

    trimmed = bcc.trim_to_budget(payload)

    assert trimmed.get("paths_omitted", 0) > 0
    assert "message_truncated" not in trimmed


@pytest.mark.parametrize("n_paths", [0, 1, 12, 13, 100, 400])
def test_trimming_always_reaches_the_budget(n_paths):
    payload = base_payload(
        paths=[f"services/deeply/nested/path/module_{i}.py" for i in range(n_paths)],
        files_changed=n_paths,
    )

    assert len(bcc.encode(bcc.trim_to_budget(payload))) <= bcc.MAX_B64_LENGTH


def test_trimming_does_not_mutate_its_input():
    payload = base_payload(paths=[f"f{i}.py" for i in range(50)])
    before = json.dumps(payload, sort_keys=True)

    bcc.trim_to_budget(payload)

    assert json.dumps(payload, sort_keys=True) == before


# --- Encoding -------------------------------------------------------------


def test_encoding_is_deterministic():
    """Constraint 6 again: identical input, identical bytes."""
    assert bcc.encode(base_payload()) == bcc.encode(base_payload())


def test_key_order_does_not_change_the_encoding():
    """Two payloads with the same content must encode identically."""
    a = {"commit_sha": SHA, "branch": "main"}
    b = {"branch": "main", "commit_sha": SHA}

    assert bcc.encode(a) == bcc.encode(b)


def test_encoded_output_contains_no_json_breaking_characters():
    """Why the payload is encoded at all.

    The result is interpolated into a JSON string in UserParameters. If it could
    contain a double quote it would terminate that string early -- which is
    exactly the bug this design avoids.
    """
    encoded = bcc.encode(base_payload(commit_message='he said "hello" and\nnewlined'))

    assert '"' not in encoded
    assert "\n" not in encoded
    assert "\\" not in encoded


def test_a_unicode_commit_message_survives_the_round_trip():
    message = "fix naïve café handling — with an em dash and 🎯"
    encoded = bcc.encode(bcc.trim_to_budget(base_payload(commit_message=message)))

    decoded = json.loads(base64.b64decode(encoded))

    assert decoded["commit_message"] == message


# --- Failure reporting ----------------------------------------------------


def test_a_payload_that_could_not_diff_carries_no_fabricated_counts():
    """`diff_stats_ok: false` and no numbers, rather than zeros."""
    payload = {
        "commit_sha": SHA,
        "commit_message": "m",
        "branch": "main",
        "author": "a",
        "committed_at": "2026-08-12T12:57:39+01:00",
        "diff_stats_ok": False,
        "diff_note": "shallow clone: no parent commit available",
    }

    trimmed = bcc.trim_to_budget(payload)

    assert trimmed["diff_stats_ok"] is False
    assert "files_changed" not in trimmed
    assert "lines_added" not in trimmed


def test_the_gate_rejects_a_no_diff_payload():
    """End to end: the build says it could not measure, the gate fails closed."""
    payload = {
        "commit_sha": SHA,
        "commit_message": "m",
        "branch": "main",
        "author": "a",
        "committed_at": "2026-08-12T12:57:39+01:00",
        "diff_stats_ok": False,
        "diff_note": "no git repository in the build",
    }
    params = {
        "trusted_commit_sha": SHA,
        "change_context_b64": bcc.encode(payload),
    }
    event = {
        "CodePipeline.job": {
            "data": {
                "actionConfiguration": {"configuration": {"UserParameters": json.dumps(params)}}
            }
        }
    }

    result = PipelineChangeContextCollector(event).collect()

    assert result.status is SignalStatus.UNAVAILABLE
    assert result.data is None
