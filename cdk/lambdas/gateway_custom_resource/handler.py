"""
CDK Custom Resource Lambda handler for AgentCore Gateway provisioning.

Manages gateway targets and Cedar policies via the policy engine API.
The gateway itself is created via native CfnGateway.

On Create  → create targets + Cedar policies
On Update  → delete old targets/policies, recreate
On Delete  → delete all targets and policies
"""

import json
import logging
import os
import time

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# ---------------------------------------------------------------------------
# Cedar policies — managed via the policy engine API
# Gateway ARN and policy engine ID are passed as properties from CDK
# ---------------------------------------------------------------------------
def _build_cedar_policies(gateway_arn: str) -> list:
    """
    Return a list of (name, description, cedar_statement, validation_mode) for all policies.
    Cedar is default-deny, so every tool needs an explicit permit.
    The forbid for return-label-generator >= 500 overrides the permit.

    Uses IGNORE_ALL_FINDINGS for the broad permit because the policy engine
    rejects unconditional permits as "overly permissive" with default validation.
    """
    return [
        # Broad permit for all authenticated users on all tools
        (
            "permit_all_other_tools",
            "Allow all authenticated users to call any tool",
            (
                f'permit(principal is AgentCore::OAuthUser, action, '
                f'resource == AgentCore::Gateway::"{gateway_arn}");'
            ),
            "IGNORE_ALL_FINDINGS",
        ),
        # Permit return-label-generator for amounts under 500
        (
            "permit_return_labels_under_500",
            "Allow return labels for orders under 500 dollars",
            (
                f'permit(principal, '
                f'action == AgentCore::Action::"return-label-generator___return-label-generator", '
                f'resource == AgentCore::Gateway::"{gateway_arn}") '
                f'when {{ context.input.amount < 500 }};'
            ),
            "FAIL_ON_ANY_FINDINGS",
        ),
        # Forbid return-label-generator for amounts >= 500
        (
            "forbid_return_labels_over_500",
            "Block return labels for orders 500 dollars or more — must escalate to human",
            (
                f'forbid(principal, '
                f'action == AgentCore::Action::"return-label-generator___return-label-generator", '
                f'resource == AgentCore::Gateway::"{gateway_arn}") '
                f'when {{ context.input.amount >= 500 }};'
            ),
            "FAIL_ON_ANY_FINDINGS",
        ),
    ]

# ---------------------------------------------------------------------------
# Tool schema definitions for all Tool Lambdas
# ---------------------------------------------------------------------------
def _build_targets(lambda_arns: dict) -> list:
    """
    Return a list of (name, description, target_config) tuples for every
    Tool Lambda that should be registered as a gateway target.
    """
    credential_config = [{"credentialProviderType": "GATEWAY_IAM_ROLE"}]

    targets = [
        (
            "order-lookup-tool",
            "Customer Order Lookup Tool",
            {
                "mcp": {
                    "lambda": {
                        "lambdaArn": lambda_arns["order_lookup"],
                        "toolSchema": {
                            "inlinePayload": [{
                                "name": "order-lookup-tool",
                                "description": "Tool to look up a customer's order by order ID.",
                                "inputSchema": {
                                    "type": "object",
                                    "properties": {
                                        "orderId": {"type": "string", "description": "The order ID to look up"},
                                    },
                                    "required": ["orderId"],
                                },
                            }]
                        },
                    }
                }
            },
        ),
        (
            "warranty-lookup-tool",
            "Order Warranty Lookup Tool",
            {
                "mcp": {
                    "lambda": {
                        "lambdaArn": lambda_arns["warranty_lookup"],
                        "toolSchema": {
                            "inlinePayload": [{
                                "name": "warranty-lookup-tool",
                                "description": "Tool to look up warranty information for an order",
                                "inputSchema": {
                                    "type": "object",
                                    "properties": {
                                        "orderId": {"type": "string"}
                                    },
                                    "required": ["orderId"],
                                },
                            }]
                        },
                    }
                }
            },
        ),
        (
            "return-label-generator",
            "Return Label Generator Tool",
            {
                "mcp": {
                    "lambda": {
                        "lambdaArn": lambda_arns["return_label_generator"],
                        "toolSchema": {
                            "inlinePayload": [{
                                "name": "return-label-generator",
                                "description": (
                                    "Tool to generate a return shipping label for an order. "
                                    "The refund is processed automatically when the item is received. "
                                    "Requires orderId and amount (integer, the order total)."
                                ),
                                "inputSchema": {
                                    "type": "object",
                                    "properties": {
                                        "orderId": {"type": "string", "description": "The order ID"},
                                        "amount": {"type": "integer", "description": "The order total amount in dollars (integer)"},
                                    },
                                    "required": ["orderId", "amount"],
                                },
                            }]
                        },
                    }
                }
            },
        ),
        (
            "company-policy-lookup",
            "Company Policy Lookup Tool",
            {
                "mcp": {
                    "lambda": {
                        "lambdaArn": lambda_arns["company_policy_lookup"],
                        "toolSchema": {
                            "inlinePayload": [{
                                "name": "company-policy-lookup",
                                "description": "Tool to look up company policy information",
                                "inputSchema": {
                                    "type": "object",
                                    "properties": {
                                        "query": {"type": "string"}
                                    },
                                    "required": ["query"],
                                },
                            }]
                        },
                    }
                }
            },
        ),
        (
            "check-return-eligibility-tool",
            "Check Return Eligibility Tool",
            {
                "mcp": {
                    "lambda": {
                        "lambdaArn": lambda_arns["check_return_eligibility"],
                        "toolSchema": {
                            "inlinePayload": [{
                                "name": "check-return-eligibility-tool",
                                "description": "Tool to check whether an order is eligible for return",
                                "inputSchema": {
                                    "type": "object",
                                    "properties": {
                                        "order_id": {"type": "string"}
                                    },
                                    "required": ["order_id"],
                                },
                            }]
                        },
                    }
                }
            },
        ),
        (
            "create-case-tool",
            "Create Escalation Case Tool",
            {
                "mcp": {
                    "lambda": {
                        "lambdaArn": lambda_arns["create_case"],
                        "toolSchema": {
                            "inlinePayload": [{
                                "name": "create-case-tool",
                                "description": "Tool to create a customer service escalation case",
                                "inputSchema": {
                                    "type": "object",
                                    "properties": {
                                        "reason": {"type": "string"},
                                    },
                                    "required": ["reason"],
                                },
                            }]
                        },
                    }
                }
            },
        ),
    ]

    # Attach credential config to every target
    return [(name, desc, cfg, credential_config) for name, desc, cfg, *_ in
            [(n, d, c, credential_config) for n, d, c in targets]]




# ---------------------------------------------------------------------------
# Main handler
# ---------------------------------------------------------------------------
def lambda_handler(event, context):
    """
    Custom resource handler for gateway targets and Cedar policy.
    The gateway itself is created via native CfnGateway.
    This handler only manages targets and the Cedar policy.
    """
    logger.info("Event: %s", json.dumps(event))

    request_type = event["RequestType"]
    props = event.get("ResourceProperties", {})

    lambda_arns = {
        "order_lookup": props["OrderLookupArn"],
        "warranty_lookup": props["WarrantyLookupArn"],
        "return_label_generator": props["ReturnLabelGeneratorArn"],
        "company_policy_lookup": props["CompanyPolicyLookupArn"],
        "check_return_eligibility": props["CheckReturnEligibilityArn"],
        "create_case": props["CreateCaseArn"],
    }
    gateway_id = props["GatewayId"]
    gateway_arn = props["GatewayArn"]
    policy_engine_id = props["PolicyEngineId"]

    client = boto3.client("bedrock-agentcore-control")

    if request_type == "Create":
        _create_targets_and_policies(client, gateway_id, gateway_arn, policy_engine_id, lambda_arns)
        return {
            "PhysicalResourceId": f"targets-{gateway_id}",
            "Data": {"GatewayId": gateway_id},
        }

    elif request_type == "Update":
        _delete_all_targets(client, gateway_id)
        _delete_all_policies(client, policy_engine_id)
        _wait_for_policies_deleted(client, policy_engine_id)
        _create_targets_and_policies(client, gateway_id, gateway_arn, policy_engine_id, lambda_arns)
        return {
            "PhysicalResourceId": f"targets-{gateway_id}",
            "Data": {"GatewayId": gateway_id},
        }

    elif request_type == "Delete":
        _delete_all_targets(client, gateway_id)
        _delete_all_policies(client, policy_engine_id)
        return {"PhysicalResourceId": event.get("PhysicalResourceId", "")}

    else:
        raise ValueError(f"Unknown RequestType: {request_type}")


# ---------------------------------------------------------------------------
# Create targets + Cedar policies
# ---------------------------------------------------------------------------
def _create_targets_and_policies(client, gateway_id, gateway_arn, policy_engine_id, lambda_arns):
    """Create all targets and Cedar policies for the gateway."""

    # Wait for gateway to be READY
    for attempt in range(60):
        gw = client.get_gateway(gatewayIdentifier=gateway_id)
        status = gw.get("status", "UNKNOWN")
        logger.info("Gateway %s status: %s (attempt %d)", gateway_id, status, attempt + 1)
        if status == "READY":
            break
        if status == "FAILED":
            raise RuntimeError(f"Gateway {gateway_id} FAILED: {gw.get('statusReasons', [])}")
        time.sleep(10)
    else:
        raise RuntimeError(f"Gateway {gateway_id} did not reach READY after 600s")

    # 1. Register tool Lambda targets
    targets = _build_targets(lambda_arns)
    for name, description, target_config, credential_config in targets:
        logger.info("Creating gateway target '%s'", name)
        client.create_gateway_target(
            gatewayIdentifier=gateway_id,
            name=name,
            description=description,
            targetConfiguration=target_config,
            credentialProviderConfigurations=credential_config,
        )
        logger.info("Target '%s' created", name)

    # 2. Create Cedar policies on the policy engine
    policies = _build_cedar_policies(gateway_arn)
    for name, description, cedar_statement, validation_mode in policies:
        logger.info("Creating Cedar policy '%s'", name)
        try:
            resp = client.create_policy(
                policyEngineId=policy_engine_id,
                name=name,
                description=description,
                definition={"cedar": {"statement": cedar_statement}},
                validationMode=validation_mode,
            )
            policy_id = resp["policyId"]
            logger.info("Policy '%s' created (id=%s), waiting for ACTIVE...", name, policy_id)
            _wait_for_policy_active(client, policy_engine_id, policy_id, name)
        except Exception as exc:
            logger.error("Failed to create policy '%s': %s", name, exc)
            raise


def _wait_for_policy_active(client, policy_engine_id, policy_id, name, timeout=120):
    """Wait for a policy to reach ACTIVE status."""
    for attempt in range(timeout // 5):
        resp = client.get_policy(policyEngineId=policy_engine_id, policyId=policy_id)
        status = resp.get("status", "UNKNOWN")
        if status == "ACTIVE":
            logger.info("Policy '%s' is ACTIVE", name)
            return
        if "FAILED" in status:
            reasons = resp.get("statusReasons", [])
            raise RuntimeError(f"Policy '{name}' failed: {reasons}")
        time.sleep(5)
    raise RuntimeError(f"Policy '{name}' did not reach ACTIVE after {timeout}s")


# ---------------------------------------------------------------------------
# Delete helpers
# ---------------------------------------------------------------------------
def _delete_all_targets(client, gateway_id):
    """Delete all targets for a gateway."""
    try:
        paginator = client.get_paginator("list_gateway_targets")
        for page in paginator.paginate(gatewayIdentifier=gateway_id):
            for target in page.get("items", []):
                tid = target["targetId"]
                logger.info("Deleting target %s", tid)
                try:
                    client.delete_gateway_target(gatewayIdentifier=gateway_id, targetId=tid)
                except Exception as exc:
                    logger.warning("Could not delete target %s: %s", tid, exc)
    except Exception as exc:
        logger.warning("Could not list targets for %s: %s", gateway_id, exc)


def _delete_all_policies(client, policy_engine_id):
    """Delete all policies on a policy engine."""
    try:
        resp = client.list_policies(policyEngineId=policy_engine_id)
        for policy in resp.get("policies", []):
            pid = policy["policyId"]
            status = policy.get("status", "")
            if "DELETING" in status:
                continue
            logger.info("Deleting policy %s (%s)", policy.get("name", ""), pid)
            try:
                client.delete_policy(policyEngineId=policy_engine_id, policyId=pid)
            except Exception as exc:
                logger.warning("Could not delete policy %s: %s", pid, exc)
    except Exception as exc:
        logger.warning("Could not list policies for %s: %s", policy_engine_id, exc)


def _wait_for_policies_deleted(client, policy_engine_id, timeout=120):
    """Wait until all policies are fully deleted."""
    for attempt in range(timeout // 5):
        resp = client.list_policies(policyEngineId=policy_engine_id)
        remaining = [p for p in resp.get("policies", []) if p.get("status") != "DELETED"]
        if not remaining:
            logger.info("All policies deleted")
            return
        logger.info("Waiting for %d policies to delete...", len(remaining))
        time.sleep(5)
    logger.warning("Some policies may not have fully deleted after %ds", timeout)
