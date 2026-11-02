"""HTTP responses in the shape API Gateway expects from a Lambda proxy integration."""
import json


def respond(status_code, body):
    return {"statusCode": status_code,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps(body)}


def error(status_code, message):
    return respond(status_code, {"error": message})