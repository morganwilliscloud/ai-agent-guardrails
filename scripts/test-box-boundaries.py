#!/usr/bin/env python3
"""Run real containment probes against an extracted Linux ARM64 agent bundle.

Usage: python scripts/test-box-boundaries.py /path/to/extracted/agent-bundle
Requires the same namespace/mount support as Box. Uses fake credentials, no AWS.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace


def main():
    bundle = Path(sys.argv[1]).resolve()
    sys.path.insert(0, str(bundle))
    import runtime_app
    runtime_app.prepare_application()
    bundle = runtime_app.APP_ROOT
    # The surrounding operator can write this copy. A refusal inside Box must
    # come from containment, not a read-only Docker mount or host ownership.
    original = (bundle / "agent.py").read_text()
    (bundle / "agent.py").write_text(original)
    with tempfile.TemporaryDirectory(prefix="box-boundaries-", dir="/var/tmp") as temp:
        directory = Path(temp)
        config = {
            "gateway-url": "https://test.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp",
            "memory-id": "test_memory-1234567890",
        }
        scratch, private = runtime_app.write_configuration(
            directory, config, "us-east-1",
            SimpleNamespace(access_key="FAKE_ACCESS", secret_key="FAKE_SECRET", token=None),
        )
        probe = scratch / "probe.py"
        probe.write_text("""
import json, os, socket, urllib.request, urllib.error
from pathlib import Path
checks = {}
Path("allowed.txt").write_text("allowed")
checks["scratch_write"] = Path("allowed.txt").read_text() == "allowed"
try:
    Path(PRIVATE_FILE).read_text()
    checks["private_credentials_denied"] = False
except OSError:
    checks["private_credentials_denied"] = True
try:
    Path(APP_FILE).write_text("tampered")
    checks["application_write_denied"] = False
except OSError:
    checks["application_write_denied"] = True
checks["aws_placeholder"] = os.environ.get("AWS_ACCESS_KEY_ID") == "AKIAIOSFODNN7EXAMPLE"
checks["customer_placeholder"] = os.environ.get("CUSTOMER_TOKEN", "").startswith("strands_box_")
try:
    socket.create_connection(("1.1.1.1", 443), timeout=3)
    checks["direct_network_denied"] = False
except OSError:
    checks["direct_network_denied"] = True
try:
    urllib.request.urlopen("https://example.com", timeout=5)
    checks["unlisted_egress_denied"] = False
except (OSError, urllib.error.URLError) as error:
    checks["unlisted_egress_denied"] = "403" in str(error)
Path("checks.json").write_text(json.dumps(checks))
""".replace("PRIVATE_FILE", repr(str(private / "credentials")))
           .replace("APP_FILE", repr(str(bundle / "agent.py"))))
        configuration = directory / "box.toml"
        lines = configuration.read_text().splitlines()
        command = [str(Path(sys.executable).resolve()), "-I", "-B", str(probe)]
        configuration.write_text("\n".join(
            "command = " + json.dumps(command) if line.startswith("command = ") else line
            for line in lines
        ) + "\n")
        env = dict(os.environ, HOME=str(private), CUSTOMER_TOKEN="FAKE_CUSTOMER_TOKEN",
                   AWS_SHARED_CREDENTIALS_FILE=str(private / "credentials"),
                   AWS_CONFIG_FILE=str(private / "config"), EXPERIMENTAL_LINUX_SUPPORT="TRUE")
        result = subprocess.run([str(bundle / "box-core/box"), "run", "--config", str(configuration)],
                                env=env, timeout=60, capture_output=True, text=True)
        if result.returncode:
            print(result.stderr[-8000:], file=sys.stderr)
            shutil.rmtree(bundle)
            return 1
        checks = json.loads((scratch / "checks.json").read_text())
        print(json.dumps(checks, indent=2))
        shutil.rmtree(bundle)
        return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
