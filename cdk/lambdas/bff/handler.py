"""
BFF Lambda — sits between API Gateway and AgentCore Runtime.
Uses IAM auth (boto3 SDK) to invoke the runtime.
Forwards the Cognito JWT in the payload so the agent code can
pass it to the AgentCore Gateway for interceptor + Cedar evaluation.
"""
import json
import logging
import os

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

RUNTIME_ARN = os.environ.get("AGENTCORE_RUNTIME_ARN", "")
REGION = os.environ.get("AWS_REGION", "us-east-1")

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Content-Type,Authorization,X-Amzn-Bedrock-AgentCore-Runtime-Session-Id",
    "Access-Control-Allow-Methods": "POST,OPTIONS",
}

_client = None


def _get_client():
    global _client
    if _client is None:
        _client = boto3.client("bedrock-agentcore", region_name=REGION)
    return _client


def lambda_handler(event, context):
    logger.info("Event keys: %s", list(event.keys()))

    try:
        body = json.loads(event.get("body", "{}"))
        message = body.get("message", body.get("prompt", ""))
        session_id = (
            event.get("headers", {}).get("x-amzn-bedrock-agentcore-runtime-session-id")
            or event.get("headers", {}).get("X-Amzn-Bedrock-AgentCore-Runtime-Session-Id")
            or "default"
        )

        # Get the Cognito JWT to forward to the agent via payload
        auth_header = event.get("headers", {}).get("Authorization", "")
        if not auth_header:
            auth_header = event.get("headers", {}).get("authorization", "")

        # Pass the JWT inside the payload so the agent code can use it
        payload = {"prompt": message}
        if auth_header:
            payload["_bearer_token"] = auth_header

        client = _get_client()
        response = client.invoke_agent_runtime(
            agentRuntimeArn=RUNTIME_ARN,
            qualifier="DEFAULT",
            payload=json.dumps(payload),
            runtimeSessionId=session_id,
        )

        response_body = response["response"].read().decode("utf-8")
        result = json.loads(response_body)
        agent_response = result.get("response", response_body)

        return {
            "statusCode": 200,
            "headers": CORS_HEADERS,
            "body": json.dumps({"response": agent_response}),
        }

    except Exception as e:
        logger.error("Error: %s", e, exc_info=True)
        return {
            "statusCode": 500,
            "headers": CORS_HEADERS,
            "body": json.dumps({"error": str(e)}),
        }
