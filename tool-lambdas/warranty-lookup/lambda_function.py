import json
import boto3
import os
from botocore.exceptions import ClientError

dynamodb = boto3.resource('dynamodb')
TABLE_NAME = os.environ.get("ORDERS_TABLE_NAME", "Orders")
table = dynamodb.Table(TABLE_NAME)

def lambda_handler(event, context):
    try:
        order_id = int(event.get("orderId"))
        customer_id = event.get("_authenticated_customer_id", "")

        response = table.get_item(Key={'orderId': order_id})
        if 'Item' not in response:
            return {"statusCode": 404, "body": json.dumps({"eligible": False, "message": f"No order found with ID {order_id}."})}

        record = response['Item']

        # Ownership check
        if customer_id and record.get("customer_id"):
            if str(record["customer_id"]) != str(customer_id):
                return {"statusCode": 403, "body": json.dumps({"eligible": False, "message": "You do not have access to this order."})}

        eligible = record.get('warrantyEligible', False)
        return {"statusCode": 200, "body": json.dumps({"eligible": eligible, "product": record["product"], "purchaseDate": record["purchaseDate"], "message": "Product is under warranty." if eligible else "Warranty period has expired."})}
    except Exception as e:
        return {"statusCode": 500, "body": json.dumps({"error": str(e)})}
