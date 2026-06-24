import json
import boto3
import uuid
import os
from datetime import datetime, timezone
from botocore.exceptions import ClientError

dynamodb = boto3.resource("dynamodb")
CASES_TABLE_NAME = os.environ.get("CASES_TABLE_NAME", "Cases")
cases_table = dynamodb.Table(CASES_TABLE_NAME)


def lambda_handler(event, context):
    try:
        # customer_id comes from the interceptor (injected from JWT), not from the agent
        customer_id = event.get("_authenticated_customer_id", "")
        reason = event.get("reason")

        if not customer_id:
            return _error(400, "customer_id is required.")
        if not reason:
            return _error(400, "reason is required.")

        case_id = f"CASE-{uuid.uuid4().hex[:12].upper()}"
        cases_table.put_item(Item={
            "case_id": case_id,
            "customer_id": str(customer_id),
            "reason": str(reason),
            "status": "open",
            "created_at": datetime.now(timezone.utc).isoformat(),
        })
        return {"statusCode": 200, "body": json.dumps({"case_id": case_id, "status": "open"})}

    except ClientError as e:
        return _error(500, f"Failed to create case: {e.response['Error']['Message']}")
    except Exception as e:
        return _error(500, f"Unexpected error: {str(e)}")


def _error(status_code: int, message: str) -> dict:
    return {"statusCode": status_code, "body": json.dumps({"error": message})}
