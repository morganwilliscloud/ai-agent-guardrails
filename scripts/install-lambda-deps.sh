#!/bin/bash
# Install vendored dependencies for Lambda functions that need them.
# Run this after cloning the repo, before cdk deploy.
set -e

echo "Installing boto3 for gateway custom resource Lambda..."
pip install -t cdk/lambdas/gateway_custom_resource -r cdk/lambdas/gateway_custom_resource/requirements.txt --upgrade --quiet

echo "Installing fpdf2 for return-label-generator Lambda..."
pip install -t tool-lambdas/return-label-generator -r tool-lambdas/return-label-generator/requirements.txt --upgrade --quiet

echo "Installing fpdf2 for issue-refund Lambda..."
pip install -t tool-lambdas/issue-refund -r tool-lambdas/issue-refund/requirements.txt --upgrade --quiet

echo "Done."
