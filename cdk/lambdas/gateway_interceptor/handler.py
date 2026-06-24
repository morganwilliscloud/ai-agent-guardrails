import json
import logging

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def lambda_handler(event, context):
    logger.info("Interceptor event: %s", json.dumps(event, default=str))

    mcp = event.get("mcp", {})
    gw_request = mcp.get("gatewayRequest", {})
    headers = gw_request.get("headers", {})
    body = gw_request.get("body", {})

    # Extract the method and tool arguments from the MCP request
    method = body.get("method", "")
    params = body.get("params", {})
    tool_name = params.get("name", "")
    tool_args = params.get("arguments", {})

    # Only check tool calls, not list/initialize
    if method != "tools/call":
        return {
            "interceptorOutputVersion": "1.0",
            "mcp": {
                "transformedGatewayRequest": {
                    "body": body,
                },
            },
        }

    # Extract authenticated customer_id from JWT in Authorization header
    authenticated_customer_id = ""
    auth_header = headers.get("Authorization", "") or headers.get("authorization", "")
    if auth_header.startswith("Bearer "):
        try:
            import base64
            token = auth_header[7:]
            payload = token.split(".")[1] + "=="
            claims = json.loads(base64.b64decode(payload))
            authenticated_customer_id = claims.get("custom:customer_id", "")
        except Exception as e:
            logger.warning("JWT decode failed: %s", e)

    # Inject authenticated customer_id into tool arguments so Lambdas can verify ownership
    logger.info("ALLOWED: tool=%s, injecting customer_id=%s", tool_name, authenticated_customer_id)
    if authenticated_customer_id and method == "tools/call":
        if "arguments" not in params:
            params["arguments"] = {}
        params["arguments"]["_authenticated_customer_id"] = authenticated_customer_id
        body["params"] = params

    return {
        "interceptorOutputVersion": "1.0",
        "mcp": {
            "transformedGatewayRequest": {
                "body": body,
            },
        },
    }
