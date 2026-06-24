# AI Agent with Layered Safety Controls

AI agent reference architecture for AnyCompany Robotics customer service.
Demonstrates layered safety controls where failures are prevented by architecture, not model behavior.

> "You cannot prompt-engineer your way out of this. You fix it with architecture."

## Architecture

```
Customer → Cognito (OAuth) → API Gateway (REST) → BFF Lambda → AgentCore Runtime (IAM)
                                                                      ↓
                                                                Bedrock Guardrails
                                                                Steering Handler
                                                                      ↓
                                                                AgentCore Gateway (CUSTOM_JWT)
                                                                 ↓          ↓
                                                          Interceptor    Cedar Policy
                                                          (JWT → ID)    (amount limits)
                                                                 ↓
                                                            Tool Lambdas
                                                            (ownership checks)
                                                                 ↓
                                                          AgentCore Memory
```

## Safety Layers

| Layer | What it does | What it catches |
|-------|-------------|-----------------|
| Cognito + API Gateway | OAuth authentication at the edge | Unauthenticated access |
| WAF | Rate limiting, common exploits | DDoS, injection patterns |
| IAM Auth on Runtime | Users cannot bypass API Gateway to invoke the agent directly | Direct runtime access |
| Bedrock Guardrails | Content filtering, PII anonymization, topic denial | Prompt attacks, off-topic requests, credit card/SSN leaks |
| Steering Handler | Reviews agent responses before delivery (LLM-as-judge) | Unconfirmed promises, leaked system details, hallucinated contact info |
| Gateway Interceptor | Extracts `custom:customer_id` from JWT, injects into tool args | Customer ID spoofing — agent never controls identity |
| Cedar Policy Engine | Declarative authorization on tool calls | Return labels for orders over $500 (must escalate to human) |
| Tool-Level Ownership | Each Lambda verifies `_authenticated_customer_id` against order data | Cross-customer data access |

## Structure

```
├── agent/                              # Strands agent (bundled + deployed via CDK)
│   ├── agent.py                        # Main agent entrypoint
│   ├── steering.py                     # Response reviewer (SteeringHandler plugin, LLM-as-judge)
│   ├── steering_deterministic.py       # Example deterministic tool steering hook (rule-based)
│   ├── basic_agent.py                  # Minimal agent for demo (no guardrails/steering)
│   └── requirements.txt                # Dependencies (bundled via Docker at deploy time)
├── cdk/                                # CDK infrastructure (everything)
│   ├── app.py
│   ├── stacks/main_stack.py            # Full stack definition
│   ├── scripts/create_zip.py           # Agent code bundling script (used by Docker)
│   └── lambdas/
│       ├── bff/                        # BFF Lambda (IAM invoke, JWT forwarding)
│       ├── gateway_custom_resource/    # Bundled boto3 for policy engine operations
│       └── gateway_interceptor/        # JWT → customer_id injection
├── tool-lambdas/                       # Tool Lambdas (invoked via AgentCore Gateway)
│   ├── order-lookup/                   # Order details with ownership check
│   ├── warranty-lookup/                # Warranty status with ownership check
│   ├── return-label-generator/         # Generates return label, verifies amount + ownership
│   ├── check-return-eligibility/       # 30-day return window check with ownership
│   ├── company-policy-lookup/          # Bedrock Knowledge Base RAG lookup
│   └── create-case/                    # Escalation case creation (customer_id from JWT)
└── frontend/                           # Single-page app with Cognito auth
    ├── index.html
    └── server.py                       # Local dev server (port 8080)
```

## What CDK Deploys

Everything is managed by `cdk deploy` — a single command deploys the full stack:

- Cognito User Pool + App Client (OAuth authorization code flow)
- REST API Gateway with Cognito authorizer + CORS
- BFF Lambda (forwards JWT in payload, invokes runtime via IAM)
- WAF (rate limiting + AWS managed rules)
- AgentCore Runtime with Docker-bundled agent code (L2 construct, dependencies installed at deploy time)
- AgentCore Gateway with CUSTOM_JWT auth + interceptor
- 6 Gateway Targets (native `CfnGatewayTarget` L1 constructs)
- Cedar Policy Engine + 3 policies (native `CfnPolicyEngine` + `CfnPolicy`)
- Policy Engine → Gateway attachment (native `CfnGateway` property)
- Bedrock Guardrails (content filters, PII anonymization, topic denial)
- AgentCore Memory
- 6 Tool Lambdas with scoped IAM permissions
- Gateway Interceptor Lambda
- DynamoDB tables (Orders, Refunds, Cases)
- S3 buckets (return labels, transcripts)
- SSM parameters for agent config

## What's NOT in CDK (manual setup)

- Demo users in Cognito (`john.smith`, `sarah.johnson`) — create via CLI after deploy
- Sample data in DynamoDB (orders) — seed via CLI or script after deploy
- CloudWatch Transaction Search — one-time account-level enablement for observability

## Prerequisites

- AWS account with Bedrock model access enabled
- Docker running (required for agent code bundling)
- Python 3.11+
- Node.js 18+ (for CDK)
- CDK bootstrapped (`cdk bootstrap`)

## Deploy

```bash
# Requires Docker running — bundles agent code with ARM64 dependencies in a container
cd cdk
pip install -r requirements.txt
cdk deploy
```

That's it. No separate `agentcore deploy` needed — CDK bundles the agent code with all dependencies using Docker and deploys it directly to the AgentCore Runtime via the L2 construct.

## Post-Deploy Setup

### Create demo users

```bash
POOL_ID=$(aws cloudformation describe-stacks --stack-name ProductionAgentGuardrailsStack \
  --query 'Stacks[0].Outputs[?OutputKey==`UserPoolId`].OutputValue' --output text)

aws cognito-idp admin-create-user --user-pool-id $POOL_ID --username john.smith \
  --user-attributes Name=email,Value=john.smith@example.com Name=custom:customer_id,Value=12345 \
  --temporary-password <PASSWORDHERE> --message-action SUPPRESS

aws cognito-idp admin-set-user-password --user-pool-id $POOL_ID --username john.smith \
  --password <PASSWORDHERE> --permanent

aws cognito-idp admin-create-user --user-pool-id $POOL_ID --username sarah.johnson \
  --user-attributes Name=email,Value=sarah.johnson@example.com Name=custom:customer_id,Value=67890 \
  --temporary-password <PASSWORDHERE> --message-action SUPPRESS

aws cognito-idp admin-set-user-password --user-pool-id $POOL_ID --username sarah.johnson \
  --password <PASSWORDHERE> --permanent
```

### Seed sample orders

```bash
aws dynamodb put-item --table-name Orders --item '{
  "orderId":{"N":"12345"},"customer_id":{"S":"12345"},"customerName":{"S":"John Smith"},
  "email":{"S":"john.smith@example.com"},"product":{"S":"RoboVac Pro X1"},
  "total":{"N":"249.99"},"purchaseDate":{"S":"2026-05-10"},
  "deliveryDate":{"S":"2026-05-13"},"shippingStatus":{"S":"delivered"},
  "warrantyEligible":{"BOOL":true}}'

aws dynamodb put-item --table-name Orders --item '{
  "orderId":{"N":"67890"},"customer_id":{"S":"67890"},"customerName":{"S":"Sarah Johnson"},
  "email":{"S":"sarah.johnson@example.com"},"product":{"S":"RoboVac Lite"},
  "total":{"N":"349.99"},"purchaseDate":{"S":"2026-05-05"},
  "deliveryDate":{"S":"2026-05-08"},"shippingStatus":{"S":"delivered"},
  "warrantyEligible":{"BOOL":false}}'

aws dynamodb put-item --table-name Orders --item '{
  "orderId":{"N":"99999"},"customer_id":{"S":"12345"},"customerName":{"S":"John Smith"},
  "email":{"S":"john.smith@example.com"},"product":{"S":"RoboVac Ultra Pro Max"},
  "total":{"N":"899.99"},"purchaseDate":{"S":"2026-05-10"},
  "deliveryDate":{"S":"2026-05-13"},"shippingStatus":{"S":"delivered"},
  "warrantyEligible":{"BOOL":true}}'
```

## Frontend

```bash
cd frontend
python server.py
# Open http://localhost:8080
```

## Key Design Decisions

- **IAM auth on runtime** — users cannot bypass API Gateway to call the agent directly
- **BFF Lambda** — invokes runtime via IAM SDK, forwards JWT in payload for downstream identity propagation
- **OAuth (CUSTOM_JWT) on the gateway** — Cedar policies evaluate the authenticated principal from the JWT
- **Single `cdk deploy`** — agent code bundled via Docker with ARM64 dependencies using the L2 construct
- **No direct refund tool** — returns generate a shipping label, refund is automatic on receipt
- **Cedar policy** blocks return labels for orders over $500 (escalated to human review)
- **Customer identity flows from JWT** through the BFF → payload → agent → gateway interceptor — the agent never controls it
- **Tools are narrow and deterministic** — the tool defines the boundary, not the model
- **All IAM permissions scoped** to specific resources (model, guardrail, SSM namespace, gateway, memory)

## Steering Approaches

The repo includes two steering implementations:

- **`steering.py`** — LLM-as-judge (probabilistic). A second model evaluates every response against a review policy before it reaches the customer. Catches unconfirmed promises, leaked system details, over-promising on escalations, and hallucinated contact info.
- **`steering_deterministic.py`** — Rule-based deterministic hook. Example of using code-based checks (regex, keyword matching) for tool call validation without an additional model call. Faster and cheaper, but less flexible.

## Troubleshooting

### Agent returns errors / no logs in CloudWatch

The AgentCore Runtime has a 30-second init timeout. If the agent code isn't properly bundled with dependencies, it fails silently. Ensure Docker is running and the bundling uses `python:3.11-slim` to match the `PYTHON_3_11` runtime.

### Docker auth required / Docker not running

The CDK Docker bundling requires Docker Desktop. If unavailable, use `agentcore deploy --auto-update-on-conflict` from the `agent/` directory as a fallback.

### Cedar policy CREATE_FAILED "Overly Restrictive"

The forbid policy must be created after the corresponding permit policy. The CDK stack has explicit `add_dependency` to enforce ordering. If deploying from scratch and this fails, delete the failed policies and redeploy.

### Gateway returns 403 on tool calls

Check the Cedar policies are all ACTIVE and the policy engine is attached to the gateway:
```bash
aws bedrock-agentcore-control get-gateway \
  --gateway-identifier <gateway-id> \
  --query 'policyEngineConfiguration'

aws bedrock-agentcore-control list-policies \
  --policy-engine-id <policy-engine-id> \
  --query 'policies[].{name:name,status:status}' --output table
```

### Memory poisoning (guardrail-blocked messages replay)

If a previous guardrail block gets stored in memory, it can poison subsequent turns in the same session. Fix: use a new session ID for each test (logout/login in the frontend). In production, implement selective memory writes.

### Fresh account deployment

1. `cdk bootstrap` (one-time)
2. `cdk deploy` (Docker must be running)
3. Run post-deploy setup (create demo users, seed DynamoDB)
4. Enable CloudWatch Transaction Search (one-time, for observability)
