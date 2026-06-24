#!/usr/bin/env python3
import aws_cdk as cdk
from stacks.main_stack import ProductionAgentGuardrailsStack

app = cdk.App()

ProductionAgentGuardrailsStack(
    app,
    "ProductionAgentGuardrailsStack",
    description="Production-ready AI agent reference architecture with layered safety controls",
)

app.synth()
