"""Tests for the demo app handler.

Runs offline with no credentials, per constraint 5 in CLAUDE.md. There is not
much to test here and that is the point -- the demo app is trivial so that a
failing pipeline is never ambiguous about where the fault is. What these tests
actually protect is the response *contract*, because the canary demo reads
`lambda_version` out of the body to show traffic shifting.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import pytest
from conftest import load_handler


@dataclass
class FakeContext:
    """Stand-in for the Lambda context object."""

    function_version: str = "7"
    aws_request_id: str = "test-request-id"


@pytest.fixture
def handler(monkeypatch):
    """Load the handler with env vars set, since they are read at import time."""
    monkeypatch.setenv("APP_VERSION", "1.2.3")
    monkeypatch.setenv("COMMIT_SHA", "abc1234")
    return load_handler("demo_app")


def test_returns_200_with_json_content_type(handler):
    response = handler.lambda_handler({}, FakeContext())

    assert response["statusCode"] == 200
    assert response["headers"]["content-type"] == "application/json"


def test_body_reports_the_lambda_version_serving_the_request(handler):
    """The canary demo depends on this field. If it moves, the demo breaks."""
    response = handler.lambda_handler({}, FakeContext(function_version="42"))
    body = json.loads(response["body"])

    assert body["lambda_version"] == "42"


def test_body_echoes_build_identity_from_the_environment(handler):
    response = handler.lambda_handler({}, FakeContext())
    body = json.loads(response["body"])

    assert body["app_version"] == "1.2.3"
    assert body["commit_sha"] == "abc1234"
    assert body["service"] == "demo-app"


def test_survives_a_context_missing_the_attributes_it_wants(handler):
    """Local invokes and some test harnesses pass a bare object as context.

    A crash here would look like an application fault during a deployment,
    which is the one thing this app must never contribute to a diagnosis.
    """

    class BareContext:
        pass

    response = handler.lambda_handler({}, BareContext())
    body = json.loads(response["body"])

    assert response["statusCode"] == 200
    assert body["lambda_version"] == "unknown"
