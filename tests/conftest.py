"""Shared test helpers.

The one thing here is `load_handler`, which exists to solve a problem that is
easy to create and unpleasant to diagnose. See FAILURES.md F-005.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

SERVICES = Path(__file__).resolve().parents[1] / "services"

# The signals package is imported normally rather than through `load_handler`,
# because it is a library rather than a Lambda entrypoint -- there is no
# `handler.py` name collision to work around (F-005), and `signals` is unique
# across the repo. Putting its parent on the path once here keeps every test
# file free of import plumbing.
_DECISION_SERVICE = str(SERVICES / "decision_service")
if _DECISION_SERVICE not in sys.path:
    sys.path.insert(0, _DECISION_SERVICE)

# The eval harness lives at the repo root rather than inside the decision
# service, because `archive_file` zips that whole directory -- anything under it
# ships to Lambda. A benchmark has no business in a production artifact, so the
# repo root goes on the path instead.
_REPO_ROOT = str(SERVICES.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# Phase 2.5. The demo app stopped being a single file when fault injection
# arrived, so its own modules have to be importable -- both for their tests and
# because handler.py now does `from faults import ...` at module scope, which
# `load_handler("demo_app")` would otherwise fail on.
#
# Safe alongside decision_service on the path because the only colliding name is
# `handler`, which nothing imports directly -- `load_handler` loads both by file
# path under distinct module names precisely to avoid that (F-005).
_DEMO_APP = str(SERVICES / "demo_app")
if _DEMO_APP not in sys.path:
    sys.path.insert(0, _DEMO_APP)


@pytest.fixture(autouse=True)
def _no_real_aws(request, monkeypatch):
    """Make a real AWS call impossible unless a test explicitly asks for one.

    CLAUDE.md constraint 5 requires the suite to run end to end with zero real
    AWS dependencies. That was true by construction until Phase 2.3, when the
    gate began building a real Inspector client by default -- at which point the
    suite quietly started calling AWS and its runtime went from 2 seconds to 88.

    Slowness was the *symptom*. The real problems are that tests then depend on
    credentials, on network, and on live account state -- so they would pass on
    this laptop and fail in CI, or worse, pass for the wrong reason.

    Opt out with `@pytest.mark.aws` for a test that genuinely intends to talk to
    AWS. There are none today.
    """
    if "aws" in request.keywords:
        return

    import boto3

    def blocked(*args, **kwargs):
        service = args[0] if args else kwargs.get("service_name", "?")
        raise RuntimeError(
            f"test tried to create a real boto3 client for {service!r}. "
            "Inject a fake client, or mark the test with @pytest.mark.aws."
        )

    monkeypatch.setattr(boto3, "client", blocked)
    monkeypatch.setattr(boto3, "resource", blocked)


def load_handler(service: str) -> ModuleType:
    """Import `services/<service>/handler.py` under a service-qualified name.

    Every Lambda in this repo names its entrypoint `handler.py`, because that is
    what the `handler.lambda_handler` runtime setting expects and it keeps the
    Terraform identical across services. The cost is that a bare
    `import handler` is ambiguous the moment there is more than one service:
    whichever test file runs first claims `sys.modules["handler"]`, and a later
    `importlib.reload()` elsewhere reloads that module rather than the intended
    one. The tests then assert against the wrong Lambda while looking correct.

    Loading by explicit path under a unique module name removes the ambiguity
    entirely, and avoids mutating `sys.path` as a side effect of importing.

    It also replaces the `importlib.reload()` pattern these tests previously
    needed. Both handlers read their configuration at module scope, so a fresh
    `exec_module` picks up the current environment -- which is precisely what a
    test that monkeypatches env vars wants, and is clearer than reloading a
    module that is already imported.
    """
    path = SERVICES / service / "handler.py"
    module_name = f"_lambda_{service}"

    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:  # pragma: no cover - import plumbing
        raise ImportError(f"could not load a module spec from {path}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module
