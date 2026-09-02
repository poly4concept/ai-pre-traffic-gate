"""Tests for the commit crafting script. Phase 2.5d.

Nothing here shells out to git. What is worth testing is not that git works, it
is that this script cannot do anything alarming with it:

  * every path stays inside demo/synthetic/
  * git is invoked as an argument list, never through a shell -- which matters
    unusually much here, because one of the recipes is deliberately a hostile
    string
  * backdating produces the weekday and hour the recipe asked for
  * the recipes still describe the risk signals they claim to
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def load(name: str) -> ModuleType:
    """Load a script by path, under a private module name.

    Same reason as conftest.load_handler (F-005): scripts are entrypoints, not
    importable modules, and two files sharing a basename would collide in
    sys.modules.
    """
    spec = importlib.util.spec_from_file_location(f"_script_{name}", SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


cc = load("craft_commit")


# --- Blast radius ---------------------------------------------------------


def test_the_synthetic_directory_is_where_it_claims_to_be():
    """The one directory this script may touch. Asserted so a refactor that
    widens it has to change a test that says why that is dangerous."""
    assert cc.SYNTHETIC_DIR.name == "synthetic"
    assert cc.SYNTHETIC_DIR.parent.name == "demo"
    assert cc.SYNTHETIC_DIR.parent.parent == Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("recipe", cc.RECIPES, ids=lambda r: r.name)
def test_every_recipe_writes_only_inside_the_synthetic_directory(recipe):
    """No absolute paths, no traversal, nothing that escapes."""
    for rel, _ in recipe.files:
        assert not Path(rel).is_absolute(), rel
        assert ".." not in Path(rel).parts, rel
        resolved = (cc.SYNTHETIC_DIR / rel).resolve()
        assert resolved.is_relative_to(cc.SYNTHETIC_DIR.resolve()), rel


def test_git_is_never_invoked_through_a_shell(monkeypatch):
    """The rule that makes a hostile commit message safe to handle.

    One recipe is literally a prompt-injection payload, and others contain
    quotes and newlines. Through a shell those become syntax; as one element of
    an argument list they stay a string.
    """
    seen = {}

    class Result:
        returncode = 0
        stdout = "ok"
        stderr = ""

    def fake_run(args, **kwargs):
        seen["args"] = args
        seen["kwargs"] = kwargs
        return Result()

    monkeypatch.setattr(cc.subprocess, "run", fake_run)
    cc.git("commit", "-m", 'evil"; rm -rf /; echo "')

    assert isinstance(seen["args"], list)
    assert seen["args"][0] == "git"
    # The payload arrives as ONE argument, untouched.
    assert seen["args"][-1] == 'evil"; rm -rf /; echo "'
    assert seen["kwargs"].get("shell") in (None, False)


def test_git_runs_in_the_repository_root(monkeypatch):
    seen = {}

    class Result:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(cc.subprocess, "run", lambda args, **kw: (seen.update(kw), Result())[1])
    cc.git("status")

    assert seen["cwd"] == cc.REPO_ROOT


def test_a_failing_git_command_raises_with_its_stderr(monkeypatch):
    class Result:
        returncode = 1
        stdout = ""
        stderr = "fatal: not a git repository"

    monkeypatch.setattr(cc.subprocess, "run", lambda *a, **k: Result())

    with pytest.raises(RuntimeError, match="not a git repository"):
        cc.git("status")


# --- Backdating -----------------------------------------------------------


@pytest.mark.parametrize(
    "recipe", [r for r in cc.RECIPES if r.weekday is not None], ids=lambda r: r.name
)
def test_backdating_lands_on_the_requested_weekday_and_hour(recipe):
    """Timestamps go through git's own author/committer date, so the whole
    downstream chain -- build script, base64 payload, `is_off_hours` -- sees a
    genuine value rather than a faked field."""
    stamp = datetime.fromisoformat(cc._commit_timestamp(recipe))

    assert stamp.weekday() == recipe.weekday
    assert stamp.hour == recipe.hour
    assert stamp.tzinfo is not None, "a naive timestamp makes is_off_hours wrong, not absent"


@pytest.mark.parametrize("recipe", cc.RECIPES, ids=lambda r: r.name)
def test_backdating_never_produces_a_future_commit(recipe):
    """A commit dated in the future is the kind of detail that derails a demo."""
    assert datetime.fromisoformat(cc._commit_timestamp(recipe)) <= datetime.now(UTC)


def test_the_off_hours_recipes_actually_land_off_hours():
    """Checked against the same rule the gate uses, not against intent."""
    from signals.types import ChangeContext, Provenance

    def is_off_hours(recipe):
        stamp = datetime.fromisoformat(cc._commit_timestamp(recipe))
        return ChangeContext(
            commit_sha="0" * 40,
            commit_message="x",
            branch="main",
            author="a",
            committed_at=stamp,
            files_changed=1,
            lines_added=1,
            lines_removed=1,
            metadata_provenance=Provenance.PIPELINE,
        ).is_off_hours

    assert is_off_hours(cc.BY_NAME["payments-friday"])
    assert is_off_hours(cc.BY_NAME["revert"])
    assert is_off_hours(cc.BY_NAME["injection"])
    assert not is_off_hours(cc.BY_NAME["safe-bump"])
    assert not is_off_hours(cc.BY_NAME["docs-only"])


# --- The recipes still mean what they claim -------------------------------


def test_recipe_names_are_unique():
    names = [r.name for r in cc.RECIPES]

    assert len(names) == len(set(names))


@pytest.mark.parametrize("recipe", cc.RECIPES, ids=lambda r: r.name)
def test_every_recipe_produces_a_real_diff(recipe):
    assert recipe.files, f"{recipe.name} writes nothing, so it cannot produce a diff"
    assert all(count > 0 for _, count in recipe.files)
    assert recipe.message.strip()


def test_line_generation_produces_roughly_the_requested_size():
    """The diff statistics are the signal, so the counts have to be real."""
    body = cc._lines("payments/settlement.py", 200)

    assert body.count("synthetic_value_") == 200


def test_markdown_files_get_markdown_content():
    """`docs-only` tests size-vs-risk, which needs the paths to look inert."""
    assert cc._lines("docs/x.md", 5).lstrip().startswith("#")
    assert "synthetic_value_" not in cc._lines("docs/x.md", 5)


def test_the_small_change_is_actually_small():
    """`safe-bump` is the over-flagging canary; if it grows, it stops testing
    what it was written to test."""
    assert sum(c for _, c in cc.BY_NAME["safe-bump"].files) <= 5


def test_the_sensitive_recipes_touch_paths_the_gate_recognises():
    """`payments/` has to appear as a substring or the signal never fires."""
    for name in ("payments-friday", "revert", "injection"):
        paths = [rel for rel, _ in cc.BY_NAME[name].files]
        assert any("payments/" in p or "auth/" in p for p in paths), name


def test_the_injection_recipe_carries_a_real_payload():
    """And is expected NEVER to come back low.

    The change is also substantial on its own -- payments/ and auth/, hundreds
    of lines, Friday evening -- so a `low` verdict cannot be reached honestly.
    It could only come from obeying the message.
    """
    recipe = cc.BY_NAME["injection"]
    message = recipe.message.lower()

    assert "ignore all previous instructions" in message
    assert "risk_level" in message
    assert "NEVER low" in " ".join(recipe.expects)
    assert sum(c for _, c in recipe.files) > 100


def test_the_revert_recipe_says_revert_in_its_first_line():
    """The word is the whole signal. An attribute scorer cannot see it; a model
    reading the message can, and that difference is the point of the scenario."""
    assert cc.BY_NAME["revert"].message.splitlines()[0].lower().startswith("revert")


def test_the_huge_refactor_is_genuinely_huge():
    recipe = cc.BY_NAME["huge-refactor"]

    assert len(recipe.files) >= 40
    assert sum(c for _, c in recipe.files) > 1000


# --- Teardown -------------------------------------------------------------


def test_cleanup_is_a_no_op_when_there_is_nothing_to_clean(monkeypatch, capsys):
    """The teardown must be safe to run twice, including on a clean checkout."""
    monkeypatch.setattr(cc, "SYNTHETIC_DIR", Path("/nonexistent/demo/synthetic"))

    assert cleanup_result(cc) == 0
    assert "Nothing to clean" in capsys.readouterr().out


def cleanup_result(module):
    return module.cleanup(push=False, dry_run=False)


def test_every_recipe_is_reachable_from_the_cli():
    """A recipe defined but not listed in `choices` is a recipe nobody can run."""
    assert set(cc.BY_NAME) == {r.name for r in cc.RECIPES}
