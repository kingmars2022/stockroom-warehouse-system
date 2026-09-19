"""End-to-end proof that the AWS integration works against real AWS.

Signs in to a real Cognito user pool, sends the resulting JWT to the locally
running API, and uploads a file through the presigned URL the API hands back.
Nothing is mocked: the token is issued by Cognito and the object lands in S3.

    python verify.py --email you@example.com --password '...'

Reads the rest from environment variables (see README).
"""

import argparse
import base64
import json
import os
import sys
import time

import boto3
import requests


def claims_of(token: str) -> dict:
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--email", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--api", default=os.environ.get("API_BASE_URL", "http://localhost:8000"))
    args = parser.parse_args()

    region = os.environ["COGNITO_REGION"]
    client_id = os.environ["COGNITO_APP_CLIENT_ID"]
    bucket = os.environ["RECEIPT_BUCKET_NAME"]

    print("1. Signing in to Cognito...")
    cognito = boto3.client("cognito-idp", region_name=region)
    auth = cognito.initiate_auth(
        ClientId=client_id,
        AuthFlow="USER_PASSWORD_AUTH",
        AuthParameters={"USERNAME": args.email, "PASSWORD": args.password},
    )
    token = auth["AuthenticationResult"]["IdToken"]
    claims = claims_of(token)
    print(f"   issuer:  {claims['iss']}")
    print(f"   subject: {claims['sub']}")
    print(f"   groups:  {claims.get('cognito:groups', [])}")
    print(f"   use:     {claims['token_use']}")

    print("2. Asking the API for a presigned upload URL...")
    body = {"filename": "verify-receipt.png", "content_type": "image/png", "size_bytes": 95}
    response = requests.post(
        f"{args.api}/api/attachments/presign",
        json=body,
        headers={"Authorization": f"Bearer {token}"},
        timeout=15,
    )
    if response.status_code != 201:
        print(f"   FAILED {response.status_code}: {response.text}")
        return 1
    intent = response.json()
    print(f"   key: {intent['key']}")

    print("3. Uploading straight to S3 through the presigned URL...")
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
    )
    upload = requests.put(
        intent["upload_url"], data=png, headers={"Content-Type": "image/png"}, timeout=15
    )
    if upload.status_code != 200:
        print(f"   FAILED {upload.status_code}: {upload.text}")
        return 1
    # The browser only ever sees this because the bucket's CORS config exposes
    # it. Without the version the API records nothing to pin, silently falls
    # back to reading whatever is current, and the checks below would pass
    # while proving none of what they claim.
    version = upload.headers.get("x-amz-version-id")
    if not version:
        print("   FAILED: no x-amz-version-id — is versioning enabled and the header exposed in CORS?")
        return 1
    print(f"   version: {version}")

    print("4. Confirming the object exists in S3...")
    s3 = boto3.client("s3", region_name=region)
    head = s3.head_object(Bucket=bucket, Key=intent["key"], VersionId=version)
    print(f"   {head['ContentLength']} bytes, {head['ContentType']}, SSE {head.get('ServerSideEncryption')}")

    print("5. Waiting for the validator Lambda to tag that version...")
    tags = wait_for_tags(s3, bucket, intent["key"], version)
    if tags is None:
        print("   FAILED: no tags after 30s — check the function's CloudWatch logs")
        return 1
    print(f"   validation={tags.get('validation')} detected-type={tags.get('detected-type')} reason={tags.get('reason')}")
    if tags.get("validation") != "passed":
        print("   FAILED: a valid PNG should have passed validation")
        return 1

    # The replacement that matters is not one that fails validation — it is one
    # that passes. Re-reading the verdict cannot catch a swap for an equally
    # valid file; only pinning the version can, so that is what is checked.
    print("6. Replacing the bytes behind the same key, then re-reading the pinned version...")
    replacement = requests.put(
        intent["upload_url"], data=png + b"\x00", headers={"Content-Type": "image/png"}, timeout=15
    )
    if replacement.status_code != 200:
        print(f"   FAILED {replacement.status_code}: {replacement.text}")
        return 1
    current = replacement.headers.get("x-amz-version-id")
    if current == version:
        print("   FAILED: the replacement reused the same version — versioning is not on")
        return 1
    print(f"   current version is now {current}, approved version is still {version}")

    pinned = {tag["Key"]: tag["Value"] for tag in
              s3.get_object_tagging(Bucket=bucket, Key=intent["key"], VersionId=version).get("TagSet", [])}
    if pinned.get("validation") != "passed":
        print(f"   FAILED: the approved version's verdict changed to {pinned.get('validation')!r}")
        return 1
    if s3.head_object(Bucket=bucket, Key=intent["key"], VersionId=version)["ContentLength"] != len(png):
        print("   FAILED: the approved version's bytes changed underneath it")
        return 1
    print("   the approved version still reads passed and still holds its original bytes")

    print("\nVerified: real Cognito token accepted by the API, real object in S3,")
    print("the S3 event triggered the Lambda that validated that version, and a")
    print("later replacement did not disturb the version that was approved.")
    return 0


def wait_for_tags(s3, bucket: str, key: str, version: str, attempts: int = 15) -> dict | None:
    """The Lambda runs on an S3 event, so the tags appear a moment after the
    upload rather than with it. Read by version: the point of the check is that
    the verdict belongs to specific bytes, not to whatever the key resolves to."""
    for _ in range(attempts):
        tagging = s3.get_object_tagging(Bucket=bucket, Key=key, VersionId=version)
        tags = {tag["Key"]: tag["Value"] for tag in tagging.get("TagSet", [])}
        if tags:
            return tags
        time.sleep(2)
    return None


if __name__ == "__main__":
    sys.exit(main())
