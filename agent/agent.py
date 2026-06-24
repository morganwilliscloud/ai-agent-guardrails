import base64
import json
import logging
import os

import boto3
from bedrock_agentcore.runtime import BedrockAgentCoreApp, RequestContext
from bedrock_agentcore.memory.integrations.strands.config import AgentCoreMemoryConfig
from bedrock_agentcore.memory.integrations.strands.session_manager import AgentCoreMemorySessionManager
from mcp.client.streamable_http import streamablehttp_client
from strands import Agent
from strands.models import BedrockModel
from strands.tools.mcp.mcp_client import MCPClient
from steering import CustomerServiceSteeringHandler

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = BedrockAgentCoreApp()

SYSTEM_PROMPT = (
    "You are the AnyCompany Robotics customer service AI. You help customers "
    "with robot vacuum products: troubleshooting, returns, warranties, "
    "and escalations.\n\n"
    "When processing a request:\n"
    "1. Look up the order if the customer provides an order ID.\n"
    "2. For returns: check eligibility first, then generate a return shipping label.\n"
    "3. The refund is processed automatically when we receive the returned item.\n"
    "4. Escalate via create-case if you cannot resolve the issue.\n\n"
    "IMPORTANT RULES:\n"
    "- The customer is already authenticated. You do NOT need their customer ID. "
    "Never ask for a customer ID. Just use the order ID they provide.\n"
    "- For returns, always generate a return label. The refund is processed "
    "automatically when we receive the item. Never offer to issue a refund directly.\n"
    "- If a return label or action is denied, do NOT promise the customer a refund will occur. "
    "Tell them a human representative will review their case and follow up with them.\n"
    "- Never guarantee an outcome you cannot deliver.\n"
    "- Never include email addresses, phone numbers, or contact information in responses. "
    "The customer already has their own contact info on file.\n"
    "- Never mention internal system names or technical details.\n\n"
    "Be warm, professional, and honest about what you can and cannot do."
)


# ── Config from SSM ──────────────────────────────────────────────────────────

_config = None

def load_config():
    global _config
    if _config is not None:
        return _config
    ssm = boto3.client("ssm", region_name=os.environ.get("AWS_REGION", "us-east-1"))
    resp = ssm.get_parameters(Names=[
        "/robot-vacuum/gateway-url",
        "/robot-vacuum/guardrail-id",
        "/robot-vacuum/memory-id",
    ])
    _config = {p["Name"].split("/")[-1]: p["Value"] for p in resp["Parameters"]}
    logger.info("Config loaded: %s", list(_config.keys()))
    return _config


# ── Auth helpers ─────────────────────────────────────────────────────────────

def extract_access_token(context):
    if not context or not hasattr(context, "request_headers") or context.request_headers is None:
        return ""
    auth = context.request_headers.get("Authorization", "")
    return auth.replace("Bearer ", "") if auth.startswith("Bearer ") else ""


def extract_actor_id(token):
    if not token:
        return "anonymous"
    try:
        claims = json.loads(base64.b64decode(token.split(".")[1] + "=="))
        return claims.get("sub", "anonymous")
    except Exception:
        return "anonymous"


def extract_session_id(context):
    if context and hasattr(context, "session_id"):
        return context.session_id
    return "default"


# ── Model ────────────────────────────────────────────────────────────────────

_model = None

def get_model():
    global _model
    if _model is None:
        cfg = load_config()
        guardrail_id = cfg.get("guardrail-id", "")
        _model = BedrockModel(
            model_id="us.anthropic.claude-sonnet-4-20250514-v1:0",
            region_name="us-east-1",
            guardrail_id=guardrail_id if guardrail_id else None,
            guardrail_version="DRAFT" if guardrail_id else None,
            guardrail_trace="enabled" if guardrail_id else None,
        )
    return _model


# ── Agent factory ────────────────────────────────────────────────────────────

def create_agent(tools, session_id, actor_id):
    cfg = load_config()
    memory_id = cfg.get("memory-id", "")

    session_manager = None
    if memory_id:
        try:
            session_manager = AgentCoreMemorySessionManager(
                agentcore_memory_config=AgentCoreMemoryConfig(
                    memory_id=memory_id,
                    session_id=session_id,
                    actor_id=actor_id,
                ),
                region_name="us-east-1",
            )
        except Exception as e:
            logger.warning("Memory setup failed: %s", e)

    return Agent(
        model=get_model(),
        tools=tools,
        system_prompt=SYSTEM_PROMPT,
        session_manager=session_manager,
        plugins=[CustomerServiceSteeringHandler()],
    )


def extract_response_text(result):
    if hasattr(result, "message") and result.message:
        parts = [b["text"] for b in result.message.get("content", []) if isinstance(b, dict) and "text" in b]
        if parts:
            return "".join(parts)
    return str(result)


# ── Entrypoint ───────────────────────────────────────────────────────────────

@app.entrypoint
def invoke(payload, context: RequestContext = None):
    prompt = payload.get("prompt", "") if payload else ""
    if not prompt:
        return {"response": "Please provide a message."}

    # Token can come from request headers (direct invoke) or payload (BFF Lambda)
    access_token = extract_access_token(context)
    if not access_token and payload:
        bearer = payload.get("_bearer_token", "")
        access_token = bearer.replace("Bearer ", "") if bearer.startswith("Bearer ") else bearer

    session_id = extract_session_id(context)
    actor_id = extract_actor_id(access_token)
    gateway_url = load_config().get("gateway-url", "")

    try:
        mcp_client = MCPClient(
            lambda: streamablehttp_client(
                gateway_url,
                headers={"Authorization": f"Bearer {access_token}"} if access_token else {},
            )
        )
        with mcp_client:
            tools = mcp_client.list_tools_sync()
            logger.info("Loaded %d tools: %s", len(tools), [t.tool_name for t in tools])


            agent = create_agent(tools, session_id, actor_id)
            result = agent(prompt)


            response_text = extract_response_text(result)
        return {"response": response_text}
    except RuntimeError as e:
        # MCP client close can throw "Connection to the MCP server was closed"
        # If we already have a response, return it
        if 'response_text' in locals() and response_text:
            return {"response": response_text}
        logger.error("Agent error: %s", e, exc_info=True)
        return {"response": "I encountered an error. Please try again."}
    except Exception as e:
        if 'response_text' in locals() and response_text:
            return {"response": response_text}
        logger.error("Agent error: %s", e, exc_info=True)
        return {"response": "I encountered an error. Please try again."}


if __name__ == "__main__":
    app.run()
