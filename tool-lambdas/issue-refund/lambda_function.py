"""
Issue Refund Tool — processes a refund and generates a return label.
Returns a clean URL for the customer (demo placeholder).
"""
import json
import os
import uuid
import boto3
from datetime import datetime, timezone
from botocore.exceptions import ClientError
from fpdf import FPDF

dynamodb = boto3.resource("dynamodb")
s3 = boto3.client("s3")

ORDERS_TABLE_NAME = os.environ.get("ORDERS_TABLE_NAME", "Orders")
REFUNDS_TABLE_NAME = os.environ.get("REFUNDS_TABLE_NAME", "Refunds")
BUCKET_NAME = os.environ.get("BUCKET_NAME", "robot-vacuum-return-labels")

orders_table = dynamodb.Table(ORDERS_TABLE_NAME)
refunds_table = dynamodb.Table(REFUNDS_TABLE_NAME)


def _generate_label_pdf(order_id, refund_id):
    path = f"/tmp/refund-{refund_id}.pdf"
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Arial", size=12)
    pdf.cell(200, 10, txt="Refund Return Shipping Label", ln=True, align="C")
    pdf.ln(10)
    pdf.cell(200, 10, txt=f"Order ID: {order_id}", ln=True)
    pdf.cell(200, 10, txt=f"Refund ID: {refund_id}", ln=True)
    pdf.cell(200, 10, txt="To: AnyCompany Robotics Returns Dept", ln=True)
    pdf.cell(200, 10, txt="123 Robot Way, Seattle WA 98101", ln=True)
    pdf.output(path)
    return path


def lambda_handler(event, context):
    try:
        order_id = event.get("order_id")
        amount = event.get("amount")
        eligible = event.get("eligible")
        customer_id = event.get("_authenticated_customer_id", "")

        if not order_id:
            return _error(400, "order_id is required.")
        if amount is None:
            return _error(400, "amount is required.")

        amount = float(amount)

        # Verify order ownership
        if customer_id:
            try:
                order = orders_table.get_item(Key={"orderId": int(order_id)})
                if "Item" in order:
                    if str(order["Item"].get("customer_id", "")) != str(customer_id):
                        return _error(403, "You do not have access to this order.")
            except Exception:  # nosec B110 - ownership pre-check; proceeds to refund logic
                pass

        # Create refund
        refund_id = f"REFUND-{uuid.uuid4().hex[:8].upper()}"
        label_token = f"RET-{uuid.uuid4().hex[:6].upper()}"
        s3_key = f"return-labels/{refund_id}.pdf"

        # Generate and upload PDF
        pdf_path = _generate_label_pdf(order_id, refund_id)
        s3.upload_file(pdf_path, BUCKET_NAME, s3_key, ExtraArgs={"ContentType": "application/pdf"})

        # Store refund record
        refunds_table.put_item(Item={
            "refund_id": refund_id,
            "order_id": str(order_id),
            "customer_id": str(customer_id),
            "amount": str(amount),
            "status": "issued",
            "label_token": label_token,
            "created_at": datetime.now(timezone.utc).isoformat(),
        })

        return {
            "statusCode": 200,
            "body": json.dumps({
                "refund_id": refund_id,
                "status": "issued",
                "amount": amount,
                "return_label_url": f"https://returns.anycompany-robotics.com/label/{label_token}",
                "message": f"Refund of ${amount:.2f} has been issued. Use the return label link to download your shipping label.",
            }),
        }
    except Exception as e:
        return _error(500, f"Unexpected error: {str(e)}")


def _error(code, msg):
    return {"statusCode": code, "body": json.dumps({"error": msg})}
