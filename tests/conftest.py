"""Shared test helpers.

The one thing here is `load_handler`, which exists to solve a problem that is
easy to create and unpleasant to diagnose. See FAILURES.md F-005.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

SERVICES = Path(__file__).resolve().parents[1] / "services"

# The signals package is imported normally rather than through `load_handler`,
# because it is a library rather than a Lambda entrypoint -- there is no
# `handler.py` name collision to work around (F-005), and `signals` is unique
# across the repo. Putting its parent on the path once here keeps every test
# file free of import plumbing.
_DECISION_SERVICE = str(SERVICES / "decision_service")
if _DECISION_SERVICE not in sys.path:
    sys.path.insert(0, _DECISION_SERVICE)


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
