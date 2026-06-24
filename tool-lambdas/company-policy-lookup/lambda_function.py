import json
import boto3
import os

bedrock = boto3.client('bedrock-agent-runtime', region_name='us-east-1')
KB_ID = os.environ.get("KB_ID", "")

def lambda_handler(event, context):
    try:
        response = bedrock.retrieve(knowledgeBaseId=KB_ID, retrievalQuery={"text": event['query']})
        return {"statusCode": 200, "body": json.dumps(response)}
    except Exception as e:
        return {"statusCode": 500, "body": json.dumps({"error": str(e)})}
