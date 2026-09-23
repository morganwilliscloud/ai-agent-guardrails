"""
Production-ready AI Agent with layered safety controls.

Architecture: Cognito → API Gateway (REST, OAuth) → AgentCore Runtime (OAuth)
              → AgentCore Gateway (CUSTOM_JWT, interceptor, Dogwood/Cedar policies)
              → Tool Lambdas
              + AgentCore Memory, Bedrock Guardrails, WAF
"""
from aws_cdk import (
    BundlingOptions, CfnOutput, DockerImage, Duration, Fn,
    Stack, RemovalPolicy,
    aws_apigateway as apigw,
    aws_bedrock as bedrock,
    aws_bedrockagentcore as agentcore,
    aws_cognito as cognito,
    aws_dynamodb as dynamodb,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_s3 as s3,
    aws_wafv2 as wafv2,
)
from constructs import Construct
import json, os


class ProductionAgentGuardrailsStack(Stack):

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        _repo = os.path.join(os.path.dirname(__file__), "..", "..")
        _tools = os.path.join(_repo, "tool-lambdas")
        _lambdas = os.path.join(os.path.dirname(__file__), "..", "lambdas")
        _cognito_issuer = f"https://cognito-idp.{self.region}.amazonaws.com"

        # ── Cognito ───────────────────────────────────────────────────────────

        self.user_pool = cognito.UserPool(self, "UserPool",
            user_pool_name="robot-vacuum-customer-service",
            self_sign_up_enabled=False,
            sign_in_aliases=cognito.SignInAliases(username=True, email=True),
            custom_attributes={"customer_id": cognito.StringAttribute(mutable=False)},
            removal_policy=RemovalPolicy.DESTROY,
        )
        self.user_pool.add_domain("Domain",
            cognito_domain=cognito.CognitoDomainOptions(
                domain_prefix=f"robot-vacuum-{self.account}",
            ),
        )
        self.app_client = self.user_pool.add_client("AppClient",
            user_pool_client_name="robot-vacuum-web",
            generate_secret=False,
            auth_flows=cognito.AuthFlow(user_password=True, admin_user_password=True),
            o_auth=cognito.OAuthSettings(
                flows=cognito.OAuthFlows(authorization_code_grant=True),
                scopes=[cognito.OAuthScope.OPENID, cognito.OAuthScope.PROFILE],
                callback_urls=["http://localhost:8080/callback", "http://localhost:8000/callback", "http://localhost:3000/callback"],
                logout_urls=["http://localhost:8080/", "http://localhost:8000/", "http://localhost:3000/"],
            ),
            id_token_validity=Duration.hours(1),
            access_token_validity=Duration.hours(1),
        )

        # ── Storage ───────────────────────────────────────────────────────────

        self.return_labels_bucket = s3.Bucket(self, "ReturnLabelsBucket",
            bucket_name=f"robot-vacuum-return-labels-{self.account}",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL, versioned=True,
            removal_policy=RemovalPolicy.DESTROY, auto_delete_objects=True,
            lifecycle_rules=[s3.LifecycleRule(id="Expire30d", enabled=True, expiration=Duration.days(30))],
        )
        self.transcripts_bucket = s3.Bucket(self, "TranscriptsBucket",
            bucket_name=f"customer-service-transcripts-{self.account}",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL, versioned=True,
            removal_policy=RemovalPolicy.DESTROY, auto_delete_objects=True,
        )
        self.orders_table = dynamodb.Table(self, "OrdersTable",
            table_name="Orders",
            partition_key=dynamodb.Attribute(name="orderId", type=dynamodb.AttributeType.NUMBER),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.DESTROY,
        )
        self.refunds_table = dynamodb.Table(self, "RefundsTable",
            table_name="Refunds",
            partition_key=dynamodb.Attribute(name="refund_id", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.DESTROY,
        )
        self.refunds_table.add_global_secondary_index(
            index_name="order_id-index",
            partition_key=dynamodb.Attribute(name="order_id", type=dynamodb.AttributeType.STRING),
            projection_type=dynamodb.ProjectionType.ALL,
        )
        self.cases_table = dynamodb.Table(self, "CasesTable",
            table_name="Cases",
            partition_key=dynamodb.Attribute(name="case_id", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.DESTROY,
        )

        # ── Tool Lambdas ─────────────────────────────────────────────────────

        _ld = dict(runtime=lambda_.Runtime.PYTHON_3_12, handler="lambda_function.lambda_handler",
                    timeout=Duration.seconds(30), memory_size=128)

        self.order_lookup_fn = lambda_.Function(self, "OrderLookup", function_name="order-lookup-tool",
            code=lambda_.Code.from_asset(os.path.join(_tools, "order-lookup")),
            environment={"ORDERS_TABLE_NAME": self.orders_table.table_name}, **_ld)
        self.orders_table.grant_read_data(self.order_lookup_fn)

        self.warranty_lookup_fn = lambda_.Function(self, "WarrantyLookup", function_name="warranty-lookup-tool",
            code=lambda_.Code.from_asset(os.path.join(_tools, "warranty-lookup")),
            environment={"ORDERS_TABLE_NAME": self.orders_table.table_name}, **_ld)
        self.orders_table.grant_read_data(self.warranty_lookup_fn)

        self.return_label_fn = lambda_.Function(self, "ReturnLabel", function_name="return-label-generator",
            code=lambda_.Code.from_asset(os.path.join(_tools, "return-label-generator")),
            environment={"BUCKET_NAME": self.return_labels_bucket.bucket_name,
                         "ORDERS_TABLE_NAME": self.orders_table.table_name}, **_ld)
        self.return_labels_bucket.grant_read_write(self.return_label_fn)
        self.orders_table.grant_read_data(self.return_label_fn)

        self.policy_lookup_fn = lambda_.Function(self, "PolicyLookup", function_name="company-policy-lookup",
            code=lambda_.Code.from_asset(os.path.join(_tools, "company-policy-lookup")),
            environment={"KB_ID": "<YOUR_KNOWLEDGE_BASE_ID>"}, **_ld)
        self.policy_lookup_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["bedrock:RetrieveAndGenerate", "bedrock:Retrieve"],
            resources=[f"arn:aws:bedrock:{self.region}:{self.account}:knowledge-base/*"]))

        self.check_eligibility_fn = lambda_.Function(self, "CheckEligibility", function_name="check-return-eligibility-tool",
            code=lambda_.Code.from_asset(os.path.join(_tools, "check-return-eligibility")),
            environment={"ORDERS_TABLE_NAME": self.orders_table.table_name}, **_ld)
        self.orders_table.grant_read_data(self.check_eligibility_fn)

        self.create_case_fn = lambda_.Function(self, "CreateCase", function_name="create-case-tool",
            code=lambda_.Code.from_asset(os.path.join(_tools, "create-case")),
            environment={"CASES_TABLE_NAME": self.cases_table.table_name}, **_ld)
        self.cases_table.grant_write_data(self.create_case_fn)

        all_tools = [self.order_lookup_fn, self.warranty_lookup_fn, self.return_label_fn,
                     self.policy_lookup_fn,
                     self.check_eligibility_fn, self.create_case_fn]

        # ── Bedrock Guardrails ────────────────────────────────────────────────

        self.guardrail = bedrock.CfnGuardrail(self, "Guardrail",
            name="robot-vacuum-guardrail",
            blocked_input_messaging="I can only help with AnyCompany Robotics robot vacuum support.",
            blocked_outputs_messaging="I can only help with AnyCompany Robotics robot vacuum support.",
            content_policy_config=bedrock.CfnGuardrail.ContentPolicyConfigProperty(filters_config=[
                bedrock.CfnGuardrail.ContentFilterConfigProperty(type="PROMPT_ATTACK", input_strength="MEDIUM", output_strength="NONE"),
                bedrock.CfnGuardrail.ContentFilterConfigProperty(type="VIOLENCE", input_strength="HIGH", output_strength="HIGH"),
                bedrock.CfnGuardrail.ContentFilterConfigProperty(type="HATE", input_strength="HIGH", output_strength="HIGH"),
                bedrock.CfnGuardrail.ContentFilterConfigProperty(type="INSULTS", input_strength="HIGH", output_strength="HIGH"),
                bedrock.CfnGuardrail.ContentFilterConfigProperty(type="MISCONDUCT", input_strength="HIGH", output_strength="HIGH"),
                bedrock.CfnGuardrail.ContentFilterConfigProperty(type="SEXUAL", input_strength="HIGH", output_strength="HIGH"),
            ]),
            sensitive_information_policy_config=bedrock.CfnGuardrail.SensitiveInformationPolicyConfigProperty(pii_entities_config=[
                bedrock.CfnGuardrail.PiiEntityConfigProperty(type="CREDIT_DEBIT_CARD_NUMBER", action="ANONYMIZE"),
                bedrock.CfnGuardrail.PiiEntityConfigProperty(type="US_SOCIAL_SECURITY_NUMBER", action="ANONYMIZE"),
            ]),
            topic_policy_config=bedrock.CfnGuardrail.TopicPolicyConfigProperty(topics_config=[
                bedrock.CfnGuardrail.TopicConfigProperty(name="CodingHelp", type="DENY",
                    definition="Requests for programming or software development help.",
                    examples=["Write me a Python script", "How do I fix this JavaScript error?"]),
                bedrock.CfnGuardrail.TopicConfigProperty(name="Weather", type="DENY",
                    definition="Requests for weather forecasts or climate info.",
                    examples=["What is the weather in Seattle?", "Will it rain tomorrow?"]),
                bedrock.CfnGuardrail.TopicConfigProperty(name="Finance", type="DENY",
                    definition="Requests for investment or financial advice.",
                    examples=["Should I buy Bitcoin?", "What stocks should I invest in?"]),
            ]),
        )

        # ── AgentCore Gateway ─────────────────────────────────────────────────

        gw_role = iam.Role(self, "GatewayRole",
            role_name="BedrockAgentCoreGatewayCustomerServiceRole",
            assumed_by=iam.ServicePrincipal("bedrock-agentcore.amazonaws.com"),
        )
        for fn in all_tools:
            fn.grant_invoke(gw_role)
        # Temporal (Dogwood) policies correlate actions across a session via the
        # Workload Access Token — the gateway role must be able to fetch it
        gw_role.add_to_policy(iam.PolicyStatement(
            actions=["bedrock-agentcore:GetWorkloadAccessToken"],
            resources=[f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:workload-identity-directory/*"]))

        # Interceptor Lambda — customer ID mismatch detection
        self.interceptor_fn = lambda_.Function(self, "Interceptor",
            function_name="gateway-interceptor",
            runtime=lambda_.Runtime.PYTHON_3_12, handler="handler.lambda_handler",
            code=lambda_.Code.from_asset(os.path.join(_lambdas, "gateway_interceptor")),
            timeout=Duration.seconds(30), memory_size=128)

        # ── Policy Engine (must be created before gateway) ─────────────────

        self.policy_engine = agentcore.PolicyEngine(self, "PolicyEngine",
            policy_engine_name="refund_policy_engine",
            description="Dogwood/Cedar policy engine for customer service tool authorization",
        )

        self.gateway = agentcore.Gateway(self, "Gateway",
            gateway_name="customer-service-agent-gateway",
            role=gw_role,
            authorizer_configuration=agentcore.GatewayAuthorizer.using_custom_jwt(
                discovery_url=f"{_cognito_issuer}/{self.user_pool.user_pool_id}/.well-known/openid-configuration",
                allowed_audience=[self.app_client.user_pool_client_id],
            ),
            interceptor_configurations=[
                agentcore.LambdaInterceptor.for_request(
                    self.interceptor_fn, pass_request_headers=True),
            ],
            exception_level=agentcore.GatewayExceptionLevel.DEBUG,
            policy_engine_configuration=agentcore.GatewayPolicyEngineConfig(
                policy_engine=self.policy_engine,
                mode=agentcore.PolicyEngineMode.ENFORCE,
            ),
        )

        # ── Gateway Targets (L2 addLambdaTarget) ───────────────────────────

        _obj = agentcore.SchemaDefinitionType.OBJECT
        _str = agentcore.SchemaDefinitionType.STRING
        _int = agentcore.SchemaDefinitionType.INTEGER
        _num = agentcore.SchemaDefinitionType.NUMBER

        self._targets = []

        def _lambda_target(lid, name, desc, fn, tool_desc, properties, required):
            t = self.gateway.add_lambda_target(lid,
                gateway_target_name=name, description=desc,
                lambda_function=fn,
                tool_schema=agentcore.ToolSchema.from_inline([
                    agentcore.ToolDefinition(
                        name=name, description=tool_desc,
                        input_schema=agentcore.SchemaDefinition(
                            type=_obj, properties=properties, required=required)),
                ]))
            self._targets.append(t)
            return t

        _lambda_target("TargetOrderLookup", "order-lookup-tool", "Customer Order Lookup Tool",
            self.order_lookup_fn, "Tool to look up a customer's order by order ID.",
            {"orderId": agentcore.SchemaDefinition(type=_str, description="The order ID to look up")},
            ["orderId"])

        _lambda_target("TargetWarrantyLookup", "warranty-lookup-tool", "Order Warranty Lookup Tool",
            self.warranty_lookup_fn, "Tool to look up warranty information for an order",
            {"orderId": agentcore.SchemaDefinition(type=_str)},
            ["orderId"])

        _lambda_target("TargetReturnLabel", "return-label-generator", "Return Label Generator Tool",
            self.return_label_fn,
            "Tool to generate a return shipping label for an order. The refund is processed automatically when the item is received. Requires orderId and amount (the order total in dollars, e.g. 249.99).",
            {"orderId": agentcore.SchemaDefinition(type=_str, description="The order ID"),
             "amount": agentcore.SchemaDefinition(type=_num, description="The order total amount in dollars (e.g. 249.99)")},
            ["orderId", "amount"])

        _lambda_target("TargetPolicyLookup", "company-policy-lookup", "Company Policy Lookup Tool",
            self.policy_lookup_fn, "Tool to look up company policy information",
            {"query": agentcore.SchemaDefinition(type=_str)},
            ["query"])

        _lambda_target("TargetCheckEligibility", "check-return-eligibility-tool", "Check Return Eligibility Tool",
            self.check_eligibility_fn, "Tool to check whether an order is eligible for return",
            {"order_id": agentcore.SchemaDefinition(type=_str)},
            ["order_id"])

        _lambda_target("TargetCreateCase", "create-case-tool", "Create Escalation Case Tool",
            self.create_case_fn, "Tool to create a customer service escalation case",
            {"reason": agentcore.SchemaDefinition(type=_str)},
            ["reason"])

        # ── Dogwood / Cedar Policies ─────────────────────────────────────
        #
        # Dogwood is a superset of Cedar: every Cedar policy is a valid Dogwood
        # policy, and Dogwood adds *temporal* operators that evaluate the
        # session's history, not just the current request. Deny-by-default:
        # a tool call is allowed only if a permit matches and no forbid overrides.

        _gw_arn = self.gateway.gateway_arn

        # Permit the read/escalation tools for any authenticated user.
        # return-label-generator is deliberately NOT in this list — its only
        # permit is the temporal policy below.
        # The engine validates each policy's actions against the gateway's tool
        # schema, so every policy must be created after the targets exist.
        permit_customer_tools = agentcore.Policy(self, "PermitCustomerTools",
            policy_engine=self.policy_engine,
            policy_name="permit_customer_tools",
            description="Allow authenticated users to call the read and escalation tools",
            statement=agentcore.PolicyStatement.from_cedar(
                'permit(principal is AgentCore::OAuthUser, action in ['
                'AgentCore::Action::"order-lookup-tool___order-lookup-tool", '
                'AgentCore::Action::"warranty-lookup-tool___warranty-lookup-tool", '
                'AgentCore::Action::"company-policy-lookup___company-policy-lookup", '
                'AgentCore::Action::"check-return-eligibility-tool___check-return-eligibility-tool", '
                'AgentCore::Action::"create-case-tool___create-case-tool"'
                f'], resource == AgentCore::Gateway::"{_gw_arn}");'),
            validation_mode=agentcore.PolicyValidationMode.IGNORE_ALL_FINDINGS,
        )

        # Temporal (Dogwood) permit: a return label is allowed only when
        #   1. the amount is under $500 (point-in-time Cedar condition), AND
        #   2. a check-return-eligibility call for the SAME order completed
        #      successfully earlier in this policy session (temporal condition).
        # The agent's system prompt asks for this ordering; this policy enforces it.
        # Temporal statements go under definition.policy (not definition.cedar),
        # which the Policy L2 does not model yet — so this one uses the L1.
        permit_labels = agentcore.CfnPolicy(self, "PermitReturnLabelsWithEligibility",
            name="permit_return_labels_with_eligibility",
            policy_engine_id=self.policy_engine.policy_engine_id,
            description="Allow return labels under 500 dollars only after a successful eligibility check for the same order in this session",
            definition=agentcore.CfnPolicy.PolicyDefinitionProperty(
                policy=agentcore.CfnPolicy.PolicyStatementProperty(
                    statement=(
                        'permit(principal, '
                        'action == AgentCore::Action::"return-label-generator___return-label-generator", '
                        f'resource == AgentCore::Gateway::"{_gw_arn}") '
                        'when { context.input.amount.lessThan(decimal("500.0")) && temporal { '
                        'formerly within 1h '
                        'AgentCore::Action::"check-return-eligibility-tool___check-return-eligibility-tool"::response{ '
                        'eventResource: resource, '
                        'input.order_id: context.input.orderId } } };'
                    ))),
        )

        # Forbid return labels >= $500 — forbid always overrides permit.
        # Must be created AFTER the permits (service rejects a forbid that
        # would make the policy set overly restrictive).
        forbid_labels = agentcore.Policy(self, "ForbidReturnLabelsOver500",
            policy_engine=self.policy_engine,
            policy_name="forbid_return_labels_over_500",
            description="Block return labels for orders 500 dollars or more — must escalate to human",
            statement=agentcore.PolicyStatement.from_cedar(
                'forbid(principal, '
                'action == AgentCore::Action::"return-label-generator___return-label-generator", '
                f'resource == AgentCore::Gateway::"{_gw_arn}") '
                'when { context.input.amount.greaterThanOrEqual(decimal("500.0")) };'),
        )
        for _policy in (permit_customer_tools, permit_labels, forbid_labels):
            for _t in self._targets:
                _policy.node.add_dependency(_t)
        forbid_labels.node.add_dependency(permit_customer_tools)
        forbid_labels.node.add_dependency(permit_labels)

        # ── AgentCore Memory ──────────────────────────────────────────────────

        mem_role = iam.Role(self, "MemoryRole",
            role_name="BedrockAgentCoreMemoryExecutionRole",
            assumed_by=iam.ServicePrincipal("bedrock-agentcore.amazonaws.com"),
        )
        self.memory = agentcore.Memory(self, "Memory",
            memory_name="robot_vacuum_memory",
            expiration_duration=Duration.days(90),
            execution_role=mem_role,
            description="Conversation memory for robot vacuum customer service agent",
        )

        # ── AgentCore Runtime ─────────────────────────────────────────────────

        # Read the create_zip script for Docker bundling
        _scripts = os.path.join(os.path.dirname(__file__), "..", "scripts")
        _agent_dir = os.path.join(_repo, "agent")

        with open(os.path.join(_scripts, "create_zip.py"), "r") as f:
            create_zip_script = f.read()

        # Package agent code + dependencies using the L2 construct with Docker bundling
        agent_artifact = agentcore.AgentRuntimeArtifact.from_code_asset(
            path=_agent_dir,
            runtime=agentcore.AgentCoreRuntime.PYTHON_3_11,
            entrypoint=["agent.py"],
            bundling=BundlingOptions(
                image=DockerImage.from_registry("python:3.11-slim"),
                command=["bash", "-c", f"""
                    set -e
                    mkdir -p /tmp/agent-bundle
                    cp -r /asset-input/* /tmp/agent-bundle/ 2>/dev/null || true
                    rm -rf /tmp/agent-bundle/__pycache__ /tmp/agent-bundle/.env \
                           /tmp/agent-bundle/.bedrock_agentcore /tmp/agent-bundle/*.pyc \
                           /tmp/agent-bundle/basic_agent.py
                    cd /tmp/agent-bundle
                    echo "Installing dependencies..."
                    pip install --target /tmp/agent-bundle --upgrade \
                        -r requirements.txt
                    touch /tmp/agent-bundle/.lock
                    cd /tmp
                    cat > /tmp/create_zip.py << 'PYEOF'
{create_zip_script}
PYEOF
                    python3 /tmp/create_zip.py
                """],
            ),
        )

        self.runtime = agentcore.Runtime(self, "Runtime",
            runtime_name="robot_vacuum_agent",
            agent_runtime_artifact=agent_artifact,
            description="Customer service agent for AnyCompany Robotics",
            environment_variables={
                "AWS_REGION_NAME": self.region,
            },
        )
        # Grant Bedrock model access (scoped to specific model and guardrail)
        # Cross-region inference profiles route to multiple regions
        self.runtime.add_to_role_policy(iam.PolicyStatement(
            actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
            resources=[
                "arn:aws:bedrock:*::foundation-model/anthropic.claude-sonnet-5",
                f"arn:aws:bedrock:{self.region}:{self.account}:inference-profile/us.anthropic.claude-sonnet-5",
            ]))
        self.runtime.add_to_role_policy(iam.PolicyStatement(
            actions=["bedrock:ApplyGuardrail"],
            resources=[Fn.sub(
                "arn:aws:bedrock:${Region}:${AccountId}:guardrail/${GuardrailId}",
                {"Region": self.region, "AccountId": self.account,
                 "GuardrailId": self.guardrail.attr_guardrail_id})]))
        # SSM access
        self.runtime.add_to_role_policy(iam.PolicyStatement(
            actions=["ssm:GetParameter", "ssm:GetParameters"],
            resources=[f"arn:aws:ssm:{self.region}:{self.account}:parameter/robot-vacuum/*"]))
        # AgentCore memory and gateway (scoped to specific resources).
        # DeleteEvent: the Strands context manager compresses conversation
        # history by rewriting memory events (delete old + create new).
        self.runtime.add_to_role_policy(iam.PolicyStatement(
            actions=["bedrock-agentcore:InvokeGateway", "bedrock-agentcore:CreateEvent",
                     "bedrock-agentcore:GetMemory", "bedrock-agentcore:ListEvents",
                     "bedrock-agentcore:DeleteEvent",
                     "bedrock-agentcore:CreateSession", "bedrock-agentcore:GetSession"],
            resources=[
                f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:gateway/*",
                f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:memory/*",
            ]))
        # X-Ray tracing for observability
        self.runtime.add_to_role_policy(iam.PolicyStatement(
            actions=["xray:PutTraceSegments", "xray:PutTelemetryRecords",
                     "xray:GetSamplingRules", "xray:GetSamplingTargets"],
            resources=[f"arn:aws:xray:{self.region}:{self.account}:*"]))

        # SSM parameters for agent config
        from aws_cdk import aws_ssm as ssm
        ssm.StringParameter(self, "SSMGatewayUrl",
            parameter_name="/robot-vacuum/gateway-url",
            string_value=self.gateway.gateway_url)
        ssm.StringParameter(self, "SSMGuardrailId",
            parameter_name="/robot-vacuum/guardrail-id",
            string_value=self.guardrail.attr_guardrail_id)
        ssm.StringParameter(self, "SSMMemoryId",
            parameter_name="/robot-vacuum/memory-id",
            string_value=self.memory.memory_id)

        # ── BFF Lambda (IAM auth to AgentCore Runtime) ─────────────────────

        self.bff_fn = lambda_.Function(self, "BffFunction",
            function_name="customer-service-bff",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset(os.path.join(_lambdas, "bff")),
            timeout=Duration.minutes(2),
            memory_size=256,
            environment={
                "AGENTCORE_RUNTIME_ARN": self.runtime.agent_runtime_arn,
            },
        )
        # IAM permission to invoke the runtime
        self.bff_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["bedrock-agentcore:InvokeAgentRuntime"],
            resources=[
                self.runtime.agent_runtime_arn,
                f"{self.runtime.agent_runtime_arn}/*",
            ]))

        # ── REST API Gateway (Cognito auth → BFF Lambda) ─────────────────

        api = apigw.RestApi(self, "Api",
            rest_api_name="customer-service-api",
            description="REST API with Cognito auth proxying to BFF Lambda",
            deploy_options=apigw.StageOptions(stage_name="prod", tracing_enabled=True),
        )

        authorizer = apigw.CognitoUserPoolsAuthorizer(self, "CognitoAuth",
            cognito_user_pools=[self.user_pool],
            authorizer_name="CognitoAuthorizer",
        )

        chat = api.root.add_resource("chat")

        # CORS preflight
        chat.add_method("OPTIONS", apigw.MockIntegration(
            integration_responses=[apigw.IntegrationResponse(
                status_code="200",
                response_parameters={
                    "method.response.header.Access-Control-Allow-Headers": "'Content-Type,Authorization,X-Amzn-Bedrock-AgentCore-Runtime-Session-Id'",
                    "method.response.header.Access-Control-Allow-Methods": "'POST,OPTIONS'",
                    "method.response.header.Access-Control-Allow-Origin": "'*'",
                },
            )],
            passthrough_behavior=apigw.PassthroughBehavior.WHEN_NO_MATCH,
            request_templates={"application/json": '{"statusCode": 200}'},
        ), method_responses=[apigw.MethodResponse(
            status_code="200",
            response_parameters={
                "method.response.header.Access-Control-Allow-Headers": True,
                "method.response.header.Access-Control-Allow-Methods": True,
                "method.response.header.Access-Control-Allow-Origin": True,
            },
        )])

        # POST /chat → BFF Lambda (Lambda proxy integration)
        chat.add_method("POST",
            apigw.LambdaIntegration(self.bff_fn),
            authorizer=authorizer,
            authorization_type=apigw.AuthorizationType.COGNITO,
        )

        # CORS gateway responses
        apigw.GatewayResponse(self, "GwRespUnauth",
            rest_api=api, type=apigw.ResponseType.UNAUTHORIZED,
            response_headers={"Access-Control-Allow-Origin": "'*'", "Access-Control-Allow-Headers": "'Content-Type,Authorization'"},
        )
        apigw.GatewayResponse(self, "GwRespDenied",
            rest_api=api, type=apigw.ResponseType.ACCESS_DENIED,
            response_headers={"Access-Control-Allow-Origin": "'*'", "Access-Control-Allow-Headers": "'Content-Type,Authorization'"},
        )
        apigw.GatewayResponse(self, "GwResp5xx",
            rest_api=api, type=apigw.ResponseType.DEFAULT_5_XX,
            response_headers={"Access-Control-Allow-Origin": "'*'", "Access-Control-Allow-Headers": "'Content-Type,Authorization'"},
        )

        # ── WAF ──────────────────────────────────────────────────────────────

        waf = wafv2.CfnWebACL(self, "WAF",
            name="customer-service-waf", scope="REGIONAL",
            default_action=wafv2.CfnWebACL.DefaultActionProperty(allow={}),
            visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                cloud_watch_metrics_enabled=True, metric_name="cs-waf", sampled_requests_enabled=True),
            rules=[
                wafv2.CfnWebACL.RuleProperty(name="CommonRules", priority=10,
                    override_action=wafv2.CfnWebACL.OverrideActionProperty(none={}),
                    statement=wafv2.CfnWebACL.StatementProperty(
                        managed_rule_group_statement=wafv2.CfnWebACL.ManagedRuleGroupStatementProperty(
                            vendor_name="AWS", name="AWSManagedRulesCommonRuleSet")),
                    visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                        cloud_watch_metrics_enabled=True, metric_name="common", sampled_requests_enabled=True)),
                wafv2.CfnWebACL.RuleProperty(name="RateLimit", priority=20,
                    action=wafv2.CfnWebACL.RuleActionProperty(block={}),
                    statement=wafv2.CfnWebACL.StatementProperty(
                        rate_based_statement=wafv2.CfnWebACL.RateBasedStatementProperty(
                            limit=100, aggregate_key_type="IP")),
                    visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                        cloud_watch_metrics_enabled=True, metric_name="rate", sampled_requests_enabled=True)),
            ],
        )
        wafv2.CfnWebACLAssociation(self, "WAFAssoc",
            resource_arn=api.deployment_stage.stage_arn,
            web_acl_arn=waf.attr_arn,
        )

        # ── Outputs ──────────────────────────────────────────────────────────

        CfnOutput(self, "ApiEndpoint", value=f"{api.url}chat")
        CfnOutput(self, "UserPoolId", value=self.user_pool.user_pool_id)
        CfnOutput(self, "ClientId", value=self.app_client.user_pool_client_id)
        CfnOutput(self, "CognitoDomain",
            value=f"https://robot-vacuum-{self.account}.auth.{self.region}.amazoncognito.com")
        CfnOutput(self, "RuntimeId", value=self.runtime.agent_runtime_id)
        CfnOutput(self, "GatewayId", value=self.gateway.gateway_id)
        CfnOutput(self, "PolicyEngineId", value=self.policy_engine.policy_engine_id)
