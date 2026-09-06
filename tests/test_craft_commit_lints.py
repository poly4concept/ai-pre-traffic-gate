"""Crafted commits survive the pipeline they exist to exercise. Phase 5.4.

WHY THIS EXISTS

`scripts/craft_commit.py` manufactures commits so the gate has realistic
changes to judge. It wrote its filler as unused local variables inside a
function:

    def synthetic() -> None:
        synthetic_value_0 = 0  # generated
        synthetic_value_1 = 1  # generated
        ...

Every one of those is ruff's F841. The repo's own `buildspec.yml` runs
`ruff check .` as the first build command, so a crafted commit carrying 180 of
them failed the **Build** stage -- and the Gate stage never ran at all. Four of
the six recipes could not produce a verdict, which is the only thing the script
is for (F-024).

It went unnoticed because `create <recipe>` was only ever run locally, where it
writes files and commits them and nothing lints the result. The two recipes that
happened to be exercised first were `safe-bump` (one line) and `docs-only`
(Markdown) -- the only two that generate no Python.

WHAT THIS ASSERTS

That the generator's OUTPUT passes the same two commands the pipeline runs.
Not that the script works, not that the commit is well-formed -- that the bytes
it writes survive CI. That is the specific thing nobody checked.

The same shape as `test_pipeline_wiring.py`: a cheap test standing at a seam
where two things have to agree, and where the feedback loop is otherwise a
failed remote build.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load_craft_commit():
    spec = importlib.util.spec_from_file_location("_cc", ROOT / "scripts" / "craft_commit.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


cc = load_craft_commit()

# Exactly what buildspec.yml runs, in the same order. Hardcoded rather than
# parsed out of the YAML so that a change to either one shows up as a visible
# diff here rather than silently altering what this test enforces.
RUFF_SELECT = "E,F,I,B,UP,S"
LINE_LENGTH = "100"


def write_recipe(recipe, root: Path) -> list[Path]:
    written = []
    for path, count in recipe.files:
        dest = root / path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(cc._lines(path, count), encoding="utf-8")
        written.append(dest)
    return written


def run_ruff(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(  # noqa: S603
        [sys.executable, "-m", "ruff", *args, "--no-cache", "--isolated"],
        capture_output=True,
        text=True,
    )


def test_there_are_recipes_to_check():
    """A loop over an empty tuple passes forever. Same guard as every other
    scanner in this suite."""
    assert len(cc.RECIPES) >= 5
    assert any(p.endswith(".py") for r in cc.RECIPES for p, _ in r.files), (
        "no recipe generates Python; this test would be checking nothing"
    )


@pytest.mark.parametrize("recipe", cc.RECIPES, ids=lambda r: r.name)
def test_generated_content_passes_ruff_check(recipe, tmp_path):
    """THE FAILURE THIS ENCODES.

    F841 on ~180 generated locals, failing the Build stage before the gate ever
    saw the commit.
    """
    write_recipe(recipe, tmp_path)

    result = run_ruff("check", "--select", RUFF_SELECT, str(tmp_path))

    assert result.returncode == 0, (
        f"recipe {recipe.name!r} generates content the repo's own CI rejects:\n"
        f"{result.stdout[:2000]}"
    )


@pytest.mark.parametrize("recipe", cc.RECIPES, ids=lambda r: r.name)
def test_generated_content_passes_ruff_format(recipe, tmp_path):
    """The buildspec runs `ruff format --check .` as well, and it fails a build
    just as hard as a lint error does."""
    write_recipe(recipe, tmp_path)

    result = run_ruff("format", "--check", "--line-length", LINE_LENGTH, str(tmp_path))

    assert result.returncode == 0, (
        f"recipe {recipe.name!r} generates unformatted content:\n{result.stdout[:2000]}"
    )


def test_the_filler_is_not_a_function_local():
    """Guards the specific fix rather than only its effect.

    `ruff check` passing is the outcome that matters, but this says WHY it
    passes -- so that someone reformatting the generator learns the constraint
    from a failing test rather than from a red pipeline.
    """
    generated = cc._lines("payments/settlement.py", 5)

    assert "def synthetic" not in generated, (
        "filler is inside a function again; every assignment becomes an unused "
        "local and F841 fails the Build stage (F-024)"
    )
    assert "SYNTHETIC_VALUE_0 = 0" in generated


def test_markdown_filler_is_still_markdown():
    """The two recipes that worked before did so because they generate no
    Python. Worth keeping that true rather than accidental."""
    generated = cc._lines("docs/thing.md", 3)

    assert generated.startswith("# Synthetic demo content")
    assert "SYNTHETIC_VALUE" not in generated
