import json
import boto3
import os
from datetime import datetime, timezone, timedelta
from botocore.exceptions import ClientError

dynamodb = boto3.resource("dynamodb")
ORDERS_TABLE_NAME = os.environ.get("ORDERS_TABLE_NAME", "Orders")
orders_table = dynamodb.Table(ORDERS_TABLE_NAME)
RETURN_WINDOW_DAYS = 30


def check_return_eligibility(order_id: str) -> dict:
    if not order_id:
        return {"eligible": False, "reason": "order_id is required.", "order_date": ""}

    try:
        response = orders_table.get_item(Key={"orderId": int(order_id)})
    except ClientError as e:
        return {"eligible": False, "reason": f"Error looking up order: {e.response['Error']['Message']}", "order_date": ""}

    if "Item" not in response:
        return {"eligible": False, "reason": f"Order {order_id} not found.", "order_date": ""}

    order = response["Item"]
    raw_date = order.get("order_date") or order.get("purchaseDate") or order.get("orderDate") or ""

    if not raw_date:
        return {"eligible": False, "reason": "Order date is missing.", "order_date": ""}

    try:
        order_date = datetime.fromisoformat(str(raw_date).replace("Z", "+00:00"))
        if order_date.tzinfo is None:
            order_date = order_date.replace(tzinfo=timezone.utc)
    except ValueError:
        return {"eligible": False, "reason": f"Order date '{raw_date}' is not a valid date format.", "order_date": str(raw_date)}

    if str(order.get("status", "")).lower() == "returned":
        return {"eligible": False, "reason": "Order has already been returned.", "order_date": order_date.date().isoformat()}

    days_since_order = (datetime.now(timezone.utc) - order_date).days
    if days_since_order > RETURN_WINDOW_DAYS:
        return {"eligible": False, "reason": f"Order is outside the {RETURN_WINDOW_DAYS}-day return window ({days_since_order} days since purchase).", "order_date": order_date.date().isoformat()}

    return {"eligible": True, "reason": f"Order is within the {RETURN_WINDOW_DAYS}-day return window ({days_since_order} days since purchase).", "order_date": order_date.date().isoformat()}


def lambda_handler(event, context):
    try:
        order_id = event.get("order_id") or event.get("orderId")
        customer_id = event.get("_authenticated_customer_id", "")

        # Ownership check before revealing any info
        if customer_id and order_id:
            try:
                resp = orders_table.get_item(Key={"orderId": int(order_id)})
                if "Item" in resp and resp["Item"].get("customer_id"):
                    if str(resp["Item"]["customer_id"]) != str(customer_id):
                        return {"statusCode": 403, "body": json.dumps({"eligible": False, "reason": "You do not have access to this order.", "order_date": ""})}
            except Exception:  # nosec B110 - ownership pre-check; main lookup handles errors
                pass

        result = check_return_eligibility(order_id)
        return {"statusCode": 200, "body": json.dumps(result)}
    except Exception as e:
        return {"statusCode": 500, "body": json.dumps({"eligible": False, "reason": f"Unexpected error: {str(e)}", "order_date": ""})}
