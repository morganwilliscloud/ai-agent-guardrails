import json
from pathlib import Path
import sys
from types import SimpleNamespace
import tomllib
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent"))
import runtime_app

CONFIG = {
    "gateway-url": "https://customer-test.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp",
    "memory-id": "test_memory-1234567890",
    "guardrail-id": "testguardrail",
}


def test_credentials_stay_outside_workload_grants(tmp_path):
    credentials = SimpleNamespace(access_key="TEST_ACCESS", secret_key="TEST_SECRET", token="TEST_SESSION")
    scratch, private = runtime_app.write_configuration(tmp_path, CONFIG, "us-east-1", credentials)
    text = (tmp_path / "box.toml").read_text()
    config = tomllib.loads(text)
    assert all(secret not in text for secret in vars(credentials).values())
    assert config["agent"]["filesystem"]["write"] == [str(scratch)]
    for grant in config["agent"]["filesystem"]["read"]:
        assert not private.is_relative_to(grant)
    assert (private / "credentials").stat().st_mode & 0o777 == 0o600
    assert config["egress"]["aws"]["secret"]["ref"] == "aws://runtime"
    assert config["egress"]["customer"]["secret"]["ref"] == "env://CUSTOMER_TOKEN"
    assert config["agent"]["command"][1:3] == ["-I", "-B"]
    assert "CUSTOMER_TOKEN" not in config["agent"]["env"]


def test_policy_uses_exact_deployment_endpoints():
    policy, *_ = runtime_app.render_policy(CONFIG, "us-east-1")
    assert "__MODEL" not in policy
    assert '"bedrock-runtime.us-east-1.amazonaws.com"' in policy
    assert '"/model/us.anthropic.claude-sonnet-5/converse-stream"' in policy
    assert '"/memories/test_memory-1234567890/*"' in policy
    assert "metadata_addresses" in policy
    assert 'Box::Action::"shell:spawn"' not in policy
    assert 'Box::Action::"fs:read"' not in policy


@pytest.mark.parametrize("url", ["http://example.com/mcp", "https://example.com/", "https://example.com:8443/mcp"])
def test_invalid_gateway_configuration_fails(url):
    with pytest.raises(ValueError):
        runtime_app.render_policy(dict(CONFIG, **{"gateway-url": url}), "us-east-1")


def test_box_failure_has_no_uncontained_fallback():
    with patch.object(runtime_app, "run_turn", side_effect=RuntimeError("sandbox setup failed")) as run:
        result = runtime_app.invoke({"prompt": "hello"}, SimpleNamespace(session_id="test"))
    assert result == {"response": "I encountered an error. Please try again."}
    run.assert_called_once()


def test_token_is_taken_from_trusted_forwarding_path():
    context = SimpleNamespace(request_headers={"Authorization": "Bearer header-token"})
    assert runtime_app.token_from_request({"_bearer_token": "Bearer payload-token"}, context) == "header-token"
    assert runtime_app.token_from_request({"_bearer_token": "Bearer payload-token"}, None) == "payload-token"


def test_missing_identity_never_launches_box():
    with patch.object(runtime_app.subprocess, "Popen") as launch:
        with pytest.raises(ValueError):
            runtime_app.run_turn({"prompt": "hello"}, SimpleNamespace(session_id="test"))
        launch.assert_not_called()


def test_invalid_subject_never_launches_box():
    with patch.object(runtime_app.subprocess, "Popen") as launch:
        with pytest.raises(ValueError, match="subject"):
            runtime_app.run_turn(
                {"prompt": "hello", "_bearer_token": "malformed"},
                SimpleNamespace(session_id="test"),
            )
        launch.assert_not_called()


def test_native_grants_use_executable_private_copy(tmp_path, monkeypatch):
    original = tmp_path / "archive"
    application = tmp_path / "application"
    turn = tmp_path / "turn"
    for directory in (original, application, turn):
        directory.mkdir()
    (original / "extension.so").write_bytes(b"fixture")
    (original / "extension.so").chmod(0o644)
    (application / "extension.so").write_bytes(b"fixture")
    (application / "extension.so").chmod(0o500)
    (application / "box_policy.dw").write_text((runtime_app.ROOT / "box_policy.dw").read_text())
    monkeypatch.setattr(runtime_app, "ROOT", original)
    monkeypatch.setattr(runtime_app, "APP_ROOT", application)
    monkeypatch.setitem(sys.modules, "_fixture_native", SimpleNamespace(__file__=str(original / "extension.so")))
    runtime_app.write_configuration(
        turn, CONFIG, "us-east-1",
        SimpleNamespace(access_key="fake", secret_key="fake", token=None),
    )
    config = tomllib.loads((turn / "box.toml").read_text())
    assert str(application / "extension.so") in config["agent"]["filesystem"]["exec"]
    assert str(original / "extension.so") not in config["agent"]["filesystem"]["exec"]
