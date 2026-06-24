"""
Return Label Generator — generates a return shipping label after verifying
order ownership and amount. The refund is processed automatically when the
item is received back.

Security:
- _authenticated_customer_id injected by the gateway interceptor from JWT
- amount verified against the actual order total (agent can't inflate)
- order ownership verified against customer_id
"""
import json
import os
import uuid
import boto3
from fpdf import FPDF

s3 = boto3.client("s3")
dynamodb = boto3.resource("dynamodb")
BUCKET_NAME = os.environ.get("BUCKET_NAME", "robot-vacuum-return-labels")
ORDERS_TABLE_NAME = os.environ.get("ORDERS_TABLE_NAME", "Orders")
orders_table = dynamodb.Table(ORDERS_TABLE_NAME)


def lambda_handler(event, context):
    try:
        order_id = event.get("orderId")
        amount = event.get("amount")
        customer_id = event.get("_authenticated_customer_id", "")

        if not order_id:
            return _error(400, "orderId is required.")
        if amount is None:
            return _error(400, "amount is required.")

        amount = float(amount)

        # Look up the order to verify ownership and amount
        try:
            resp = orders_table.get_item(Key={"orderId": int(order_id)})
        except Exception:
            return _error(404, f"Order {order_id} not found.")

        if "Item" not in resp:
            return _error(404, f"Order {order_id} not found.")

        order = resp["Item"]

        # Ownership check
        if customer_id and order.get("customer_id"):
            if str(order["customer_id"]) != str(customer_id):
                return _error(403, "You do not have access to this order.")

        # Amount verification — must match the actual order total
        actual_total = float(order.get("total", 0))
        if abs(amount - actual_total) > 0.01:
            return _error(400, f"Amount ${amount:.2f} does not match order total of ${actual_total:.2f}.")

        # Generate the return label PDF
        label_token = f"RET-{uuid.uuid4().hex[:6].upper()}"
        filename = f"return-labels/{label_token}.pdf"
        filepath = f"/tmp/{label_token}.pdf"

        pdf = FPDF()
        pdf.add_page()
        pdf.set_font("Arial", size=12)
        pdf.cell(200, 10, txt="Return Shipping Label", ln=True, align="C")
        pdf.ln(10)
        pdf.cell(200, 10, txt=f"Order ID: {order_id}", ln=True)
        pdf.cell(200, 10, txt=f"Return Code: {label_token}", ln=True)
        pdf.cell(200, 10, txt=f"Refund Amount: ${actual_total:.2f} (processed on receipt)", ln=True)
        pdf.cell(200, 10, txt="To: AnyCompany Robotics Returns Dept", ln=True)
        pdf.cell(200, 10, txt="123 Robot Way, Seattle WA 98101", ln=True)
        pdf.output(filepath)

        s3.upload_file(filepath, BUCKET_NAME, filename, ExtraArgs={"ContentType": "application/pdf"})

        return {
            "statusCode": 200,
            "body": json.dumps({
                "orderId": order_id,
                "return_label_url": f"https://returns.anycompany-robotics.com/label/{label_token}",
                "refund_amount": actual_total,
                "message": f"Return label generated. Your refund of ${actual_total:.2f} will be processed automatically when we receive the item.",
            }),
        }
    except Exception as e:
        return _error(500, f"Unexpected error: {str(e)}")


def _error(code, msg):
    return {"statusCode": code, "body": json.dumps({"error": msg})}
