"""Trusted HTTP entrypoint: credentials stay here and in Box's egress gateway."""
import json
import logging
import os
from pathlib import Path
import pwd
import shutil
import signal
import subprocess
import sys
import sysconfig
import tempfile
import threading
from urllib.parse import urlsplit

import boto3
from bedrock_agentcore.runtime import BedrockAgentCoreApp, RequestContext

ROOT = Path(__file__).resolve().parent
APP_ROOT = ROOT
RUN_LOCK = threading.Lock()
app = BedrockAgentCoreApp()
logger = logging.getLogger(__name__)
MODEL_ID = "us.anthropic.claude-sonnet-5"
TURN_TIMEOUT = 75


def prepare_application():
    """Managed code archives may not retain executable modes. Copy privately."""
    global APP_ROOT
    destination = Path(tempfile.mkdtemp(prefix="guardrails-app-", dir="/var/tmp"))
    shutil.copytree(ROOT, destination, dirs_exist_ok=True)
    for name in ("box", "strands-box-contain-trampoline", "strands-box-sock-alias"):
        (destination / "box-core" / name).chmod(0o500)
    for extension in destination.rglob("*.so"):
        extension.chmod(0o500)
    # Runtime V2 can start managed code as root. Box uses a non-root operator.
    if os.getuid() == 0:
        operator = pwd.getpwnam("nobody")
        for path in (destination, *destination.rglob("*")):
            os.chown(path, operator.pw_uid, operator.pw_gid, follow_symlinks=False)
        os.setgroups([])
        os.setgid(operator.pw_gid)
        os.setuid(operator.pw_uid)
    pwd.getpwuid(os.getuid())  # Box needs a real passwd entry for its access floors.
    APP_ROOT = destination


def token_from_request(payload, context):
    headers = getattr(context, "request_headers", None) or {}
    token = headers.get("Authorization") or headers.get("authorization") or payload.get("_bearer_token", "")
    return token.removeprefix("Bearer ")


def render_policy(config, region):
    gateway = urlsplit(config["gateway-url"])
    if gateway.scheme != "https" or gateway.path != "/mcp" or gateway.port not in (None, 443):
        raise ValueError("Expected an HTTPS AgentCore Gateway /mcp endpoint")
    model_host = f"bedrock-runtime.{region}.amazonaws.com"
    memory_host = f"bedrock-agentcore.{region}.amazonaws.com"
    memory_path = "/memories/" + config["memory-id"]
    substitutions = {
        "MODEL_HOST": model_host, "MEMORY_HOST": memory_host, "GATEWAY_HOST": gateway.hostname,
        "CONVERSE_PATH": f"/model/{MODEL_ID}/converse",
        "STREAM_PATH": f"/model/{MODEL_ID}/converse-stream",
        "MEMORY_PATH": memory_path, "MEMORY_CHILDREN": memory_path + "/*",
    }
    policy = (APP_ROOT / "box_policy.dw").read_text()
    for key, value in substitutions.items():
        policy = policy.replace("__" + key + "__", json.dumps(value))
    return policy, model_host, memory_host, gateway.hostname


def write_configuration(directory, config, region, credentials):
    # Import the worker's SDK dependencies before discovering native modules.
    import agent  # noqa: F401
    scratch = directory / "workspace"
    scratch.mkdir()
    private = directory / "operator"
    private.mkdir()
    credential_file = private / "credentials"
    text = f"[runtime]\naws_access_key_id = {credentials.access_key}\naws_secret_access_key = {credentials.secret_key}\n"
    if credentials.token:
        text += f"aws_session_token = {credentials.token}\n"
    credential_file.write_text(text)
    credential_file.chmod(0o600)
    (private / "config").write_text("")
    policy, model_host, memory_host, gateway_host = render_policy(config, region)
    (directory / "policy.dw").write_text(policy)
    libraries = {
        Path(p).resolve() for p in (
            sysconfig.get_path("stdlib"), sysconfig.get_path("platstdlib"),
            sysconfig.get_config_var("LIBDIR"), sysconfig.get_config_var("DESTSHARED"),
            "/usr/lib", "/usr/lib64",
        ) if p and Path(p).is_dir()
    }
    # Native extensions need executable mappings, but application source is read-only.
    native_modules = []
    for module in list(sys.modules.values()):
        filename = getattr(module, "__file__", "") or ""
        if filename.endswith(".so"):
            path = Path(filename).resolve()
            # Use the private executable copy, not the original archive's
            # potentially non-executable files imported by the HTTP host.
            if path.is_relative_to(ROOT):
                path = APP_ROOT / path.relative_to(ROOT)
            native_modules.append(path)
    extensions = sorted({
        str(p.resolve()) for p in [*APP_ROOT.rglob("*.so"), *native_modules]
        if p.is_file() and p.stat().st_mode & 0o111
    })
    env = {
        "HOME": str(scratch), "TMPDIR": str(scratch), "PATH": "/usr/bin:/bin",
        "AWS_REGION": region, "AWS_DEFAULT_REGION": region,
        "AWS_EC2_METADATA_DISABLED": "true", "OTEL_SDK_DISABLED": "true",
        "BEDROCK_MODEL_ID": MODEL_ID,
    }
    read_paths = {APP_ROOT, scratch, *libraries}
    read_paths = {p for p in read_paths if not any(q in p.parents for q in read_paths)}
    lines = [
        'name = "customer-service"', 'box_dir = ' + json.dumps(str(directory / "state")),
        'policy = "policy.dw"', "[agent]",
        "command = " + json.dumps([str(Path(sys.executable).resolve()), "-I", "-B",
                                  str(APP_ROOT / "boxed_worker.py"),
                                  str(scratch / "request.json"), str(scratch / "response.json")]),
        "workspace = " + json.dumps(str(scratch)),
        "env = { " + ", ".join(k + " = " + json.dumps(v) for k, v in env.items()) + " }",
        "[agent.filesystem]",
        "read = " + json.dumps(sorted(str(p) for p in read_paths)),
        "write = " + json.dumps([str(scratch)]),
        "exec = " + json.dumps(extensions),
        "[egress.aws]", "destinations = " + json.dumps([model_host, memory_host]),
        'secret.ref = "aws://runtime"',
        "[egress.customer]", "destinations = " + json.dumps([gateway_host]),
        'secret.ref = "env://CUSTOMER_TOKEN"',
    ]
    (directory / "box.toml").write_text("\n".join(lines) + "\n")
    return scratch, private


def stop_box(process):
    """Box handles TERM and reaps its workload. KILL the group if it cannot exit."""
    os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)


def run_turn(payload, context):
    from agent import extract_actor_id, load_config
    token = token_from_request(payload, context)
    session_id = getattr(context, "session_id", None)
    if not token or not session_id:
        raise ValueError("An authenticated customer and runtime session are required")
    actor_id = extract_actor_id(token)
    if not isinstance(actor_id, str) or actor_id == "anonymous" or not actor_id:
        raise ValueError("The customer token must contain a subject")
    config = load_config()
    region = os.environ.get("AWS_REGION", "us-east-1")
    credentials = boto3.Session().get_credentials().get_frozen_credentials()
    with tempfile.TemporaryDirectory(prefix="guardrails-turn-", dir="/var/tmp") as temp:
        directory = Path(temp)
        scratch, private = write_configuration(directory, config, region, credentials)
        (scratch / "request.json").write_text(json.dumps({
            "prompt": payload["prompt"], "config": config,
            "session_id": session_id, "actor_id": actor_id,
        }))
        # Explicit host environment. The workload receives only Box's configured
        # environment and credential placeholders, never the original credentials.
        env = {
            "HOME": str(private), "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "AWS_SHARED_CREDENTIALS_FILE": str(private / "credentials"),
            "AWS_CONFIG_FILE": str(private / "config"), "CUSTOMER_TOKEN": token,
            "EXPERIMENTAL_LINUX_SUPPORT": "TRUE",
        }
        with (private / "stdout").open("w") as stdout, (private / "stderr").open("w") as stderr:
            process = subprocess.Popen(
                [str(APP_ROOT / "box-core/box"), "run", "--config", str(directory / "box.toml")],
                env=env, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                start_new_session=True,
            )
            try:
                process.wait(timeout=TURN_TIMEOUT)
            except subprocess.TimeoutExpired:
                stop_box(process)
                raise RuntimeError("Box turn timed out") from None
        if process.returncode:
            # Keep credentials and customer content out of normal CloudWatch logs.
            diagnostic = (private / "stderr").read_text()
            for secret in (token, credentials.access_key, credentials.secret_key, credentials.token):
                if secret:
                    diagnostic = diagnostic.replace(secret, "[REDACTED]")
            logger.error("Box exited %s: %s", process.returncode, diagnostic[-4000:])
            raise RuntimeError("Box turn failed")
        response = json.loads((scratch / "response.json").read_text())
        if not isinstance(response.get("response"), str):
            raise RuntimeError("Invalid worker response")
        logger.info("Box turn completed successfully")
        return response


@app.entrypoint
def invoke(payload, context: RequestContext = None):
    if not isinstance(payload, dict) or not isinstance(payload.get("prompt"), str) or not payload["prompt"]:
        return {"response": "Please provide a message."}
    with RUN_LOCK:
        try:
            return run_turn(payload, context)
        except Exception:
            logger.exception("Contained agent invocation failed")
            # Fail closed: never retry by running the agent outside Box.
            return {"response": "I encountered an error. Please try again."}


if __name__ == "__main__":
    prepare_application()
    app.run()
