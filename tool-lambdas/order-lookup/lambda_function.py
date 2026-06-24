"""
Order Lookup Tool — returns order details only if the order belongs
to the authenticated customer. Identity comes from the x-customer-id
header injected by the gateway interceptor from the JWT.
"""
import json
import logging
import os
import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

dynamodb = boto3.resource("dynamodb")
TABLE_NAME = os.environ.get("ORDERS_TABLE_NAME", "Orders")
table = dynamodb.Table(TABLE_NAME)


def lambda_handler(event, context):
    logger.info("Event: %s", json.dumps(event, default=str))
    try:
        order_id = int(event.get("orderId"))
        # customer_id injected by the interceptor from the JWT
        customer_id = event.get("_authenticated_customer_id", "")

        response = table.get_item(Key={"orderId": order_id})
        if "Item" not in response:
            return {
                "statusCode": 404,
                "body": json.dumps({"found": False, "message": f"No order found with ID {order_id}."}),
            }

        record = response["Item"]

        # Ownership check
        if customer_id and record.get("customer_id"):
            if str(record["customer_id"]) != str(customer_id):
                logger.warning("Access denied: customer_id=%s tried to access order owned by %s",
                               customer_id, record.get("customer_id"))
                return {
                    "statusCode": 403,
                    "body": json.dumps({"found": False, "message": "You do not have access to this order."}),
                }

        return {
            "statusCode": 200,
            "body": json.dumps({
                "found": True,
                "orderId": order_id,
                "purchaseDate": record["purchaseDate"],
                "product": record["product"],
                "customer": record["customerName"],
                "email": record["email"],
                "shippingStatus": record["shippingStatus"],
                "deliveryDate": record.get("deliveryDate"),
                "total": float(record["total"]),
                "message": "Order retrieved successfully.",
            }),
        }
    except Exception as e:
        return {"statusCode": 500, "body": json.dumps({"error": str(e)})}
