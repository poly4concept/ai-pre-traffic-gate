"""Tests for the canary driver's AppSpec construction.

Only the AppSpec is tested here. The rest of the script is AWS API orchestration
whose value is in running against real CodeDeploy, and mocking it would assert
that the mocks match my assumptions rather than that the deployment works.

The AppSpec is different: it is a literal document with exact requirements, it is
constructed rather than fetched, and getting it subtly wrong produces a
`create-deployment` rejection with a message that does not name the offending
field. Worth pinning.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType


def load_script() -> ModuleType:
    """Load scripts/deploy_canary.py by path, under its own module name."""
    path = Path(__file__).resolve().parents[1] / "scripts" / "deploy_canary.py"
    spec = importlib.util.spec_from_file_location("_script_deploy_canary", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["_script_deploy_canary"] = module
    spec.loader.exec_module(module)
    return module


script = load_script()


def test_appspec_version_is_a_number_not_a_string():
    """CodeDeploy rejects a quoted version. This is the easiest thing to get wrong.

    `json.dumps({"version": 0.0})` gives `0.0`; `{"version": "0.0"}` gives
    `"0.0"`, and the resulting rejection does not say which field it disliked.
    """
    raw = script.build_appspec("fn", "live", "1", "2")

    assert '"version": 0.0' in raw
    assert '"version": "0.0"' not in raw
    assert isinstance(json.loads(raw)["version"], float)


def test_appspec_names_the_function_alias_and_both_versions():
    """The deployment TARGET lives here, not in the deployment group."""
    parsed = json.loads(script.build_appspec("my-fn", "live", "3", "4"))
    props = parsed["Resources"][0]["demo_app"]["Properties"]

    assert props["Name"] == "my-fn"
    assert props["Alias"] == "live"
    assert props["CurrentVersion"] == "3"
    assert props["TargetVersion"] == "4"
    assert parsed["Resources"][0]["demo_app"]["Type"] == "AWS::Lambda::Function"


def test_appspec_versions_are_strings():
    """CodeDeploy expects version numbers as strings, unlike `version` itself."""
    props = json.loads(script.build_appspec("fn", "live", "1", "2"))["Resources"][0]["demo_app"][
        "Properties"
    ]

    assert isinstance(props["CurrentVersion"], str)
    assert isinstance(props["TargetVersion"], str)


def test_render_split_reports_percentages_per_version():
    from collections import Counter

    rendered = script.render_split(Counter({"1": 18, "2": 2}), 20)

    assert "v1=90%" in rendered
    assert "v2=10%" in rendered


def test_render_split_handles_zero_samples():
    from collections import Counter

    assert script.render_split(Counter(), 0) == "no samples"
