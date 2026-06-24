"""
Tiny custom resource: attach/detach a policy engine to/from an AgentCore Gateway.
This is the one thing CloudFormation doesn't support natively yet.
Uses the bundled boto3 in this directory which has the policyEngineConfiguration API.
"""
import json
import logging
import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def lambda_handler(event, context):
    logger.info("Event: %s", json.dumps(event))
    props = event.get("ResourceProperties", {})
    gw_id = props["GatewayId"]
    pe_arn = props["PolicyEngineArn"]
    mode = props.get("Mode", "ENFORCE")
    client = boto3.client("bedrock-agentcore-control")

    if event["RequestType"] in ("Create", "Update"):
        gw = client.get_gateway(gatewayIdentifier=gw_id)
        client.update_gateway(
            gatewayIdentifier=gw_id,
            name=gw["name"],
            roleArn=gw["roleArn"],
            protocolType=gw["protocolType"],
            authorizerType=gw["authorizerType"],
            authorizerConfiguration=gw.get("authorizerConfiguration", {}),
            interceptorConfigurations=gw.get("interceptorConfigurations", []),
            policyEngineConfiguration={"arn": pe_arn, "mode": mode},
        )
        logger.info("Attached policy engine %s to gateway %s (%s)", pe_arn, gw_id, mode)

    elif event["RequestType"] == "Delete":
        try:
            gw = client.get_gateway(gatewayIdentifier=gw_id)
            client.update_gateway(
                gatewayIdentifier=gw_id,
                name=gw["name"],
                roleArn=gw["roleArn"],
                protocolType=gw["protocolType"],
                authorizerType=gw["authorizerType"],
                authorizerConfiguration=gw.get("authorizerConfiguration", {}),
                interceptorConfigurations=gw.get("interceptorConfigurations", []),
            )
            logger.info("Detached policy engine from gateway %s", gw_id)
        except Exception as e:
            logger.warning("Could not detach policy engine: %s", e)

    return {"PhysicalResourceId": f"pe-attach-{gw_id}", "Data": {"GatewayId": gw_id}}
