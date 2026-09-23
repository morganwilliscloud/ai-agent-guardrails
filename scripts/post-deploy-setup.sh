#!/usr/bin/env bash
# Post-deploy setup: demo users + sample orders.
# Usage: DEMO_PASSWORD='YourPassword1!' ./scripts/post-deploy-setup.sh
set -euo pipefail

STACK_NAME="${STACK_NAME:-ProductionAgentGuardrailsStack}"
DEMO_PASSWORD="${DEMO_PASSWORD:?set DEMO_PASSWORD}"

POOL_ID=$(aws cloudformation describe-stacks --stack-name "$STACK_NAME" \
  --query 'Stacks[0].Outputs[?OutputKey==`UserPoolId`].OutputValue' --output text)

create_user() {
  local username=$1 email=$2 customer_id=$3
  aws cognito-idp admin-create-user --user-pool-id "$POOL_ID" --username "$username" \
    --user-attributes Name=email,Value="$email" Name=custom:customer_id,Value="$customer_id" \
    --temporary-password "$DEMO_PASSWORD" --message-action SUPPRESS >/dev/null
  aws cognito-idp admin-set-user-password --user-pool-id "$POOL_ID" --username "$username" \
    --password "$DEMO_PASSWORD" --permanent
  echo "created user $username (customer_id=$customer_id)"
}

create_user john.smith john.smith@example.com 12345
create_user sarah.johnson sarah.johnson@example.com 67890

# Recent purchase dates keep the orders inside the 30-day return window.
RECENT=$(date -v-7d +%Y-%m-%d 2>/dev/null || date -d '7 days ago' +%Y-%m-%d)
DELIVERED=$(date -v-4d +%Y-%m-%d 2>/dev/null || date -d '4 days ago' +%Y-%m-%d)

seed_order() {
  local order_id=$1 customer_id=$2 name=$3 email=$4 product=$5 total=$6 warranty=$7
  aws dynamodb put-item --table-name Orders --item "{
    \"orderId\":{\"N\":\"$order_id\"},\"customer_id\":{\"S\":\"$customer_id\"},
    \"customerName\":{\"S\":\"$name\"},\"email\":{\"S\":\"$email\"},
    \"product\":{\"S\":\"$product\"},\"total\":{\"N\":\"$total\"},
    \"purchaseDate\":{\"S\":\"$RECENT\"},\"deliveryDate\":{\"S\":\"$DELIVERED\"},
    \"shippingStatus\":{\"S\":\"delivered\"},\"warrantyEligible\":{\"BOOL\":$warranty}}"
  echo "seeded order $order_id ($product, \$$total)"
}

seed_order 12345 12345 "John Smith"    john.smith@example.com    "RoboVac Pro X1"        249.99 true
seed_order 67890 67890 "Sarah Johnson" sarah.johnson@example.com "RoboVac Lite"          349.99 false
seed_order 99999 12345 "John Smith"    john.smith@example.com    "RoboVac Ultra Pro Max" 899.99 true

echo "done"
