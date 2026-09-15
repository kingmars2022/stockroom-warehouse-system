# Free-tier verification stack

A deliberately small slice of the AWS foundation, built to be stood up, proven
to work, and torn down the same afternoon at no cost.

The [production stack](../) is the real design: RDS in private subnets, ECR,
SNS, deletion protection, final snapshots. It is also the expensive part, and
its safety settings make it awkward to destroy on purpose. This stack exists
for the opposite job — proving the application's AWS integration actually runs
against AWS, not against a mock.

It creates only two things:

| Resource | Why it is here |
|---|---|
| Cognito user pool, client, 3 groups | Exercises [`auth.py`](../../../backend/app/auth.py) — real RS256 tokens, real JWKS, real `cognito:groups` |
| Private S3 bucket (SSE, CORS, public access blocked) | Exercises the presigned upload path in [`main.py`](../../../backend/app/main.py) |
| Receipt-validator Lambda + S3 event notification | Closes a real gap: presign can only check what the client *declares*, and the browser uploads straight to S3. The function reads the first bytes on `ObjectCreated`, compares the real signature against the declared type, and tags the verdict. [`handler.py`](../../../backend/lambdas/receipt_validator/handler.py) |
| API Gateway (HTTP API) + price-webhook Lambda | Lets suppliers push price quotes without a Cognito account. HMAC-signed, throttled, write-only into its own S3 prefix, and with no route to the database. [`handler.py`](../../../backend/lambdas/supplier_price_webhook/handler.py) |

Those are the services the application code actually talks to. PostgreSQL stays
local in Docker — the API only sees a connection string, so running it on RDS
proves nothing the local database does not.

## Cost

Everything here sits far inside free allowances at this usage: a handful of
users, a few kilobytes of objects, and Lambda invocations against a monthly
allowance of a million requests. Everything that actually costs money — RDS,
NAT gateways, load balancers, App Runner — is excluded by design, not by luck.

Two deliberate choices keep it that way:

* Both functions declare their CloudWatch log group explicitly with a 7-day
  retention. An implicitly created group keeps logs forever, and that is the
  likeliest way this stack could quietly start billing.
* The webhook's shared secret is a Terraform-generated value passed as an
  environment variable rather than a Secrets Manager entry, because Secrets
  Manager bills per secret per month. Production should use it; a stack whose
  whole point is costing nothing should not.

The API Gateway stage sets a request throttle. A public endpoint with no limit
is a way to be billed by strangers.

That said, free is something you confirm, not something you assume. Step 1 is
not optional.

> AWS App Runner stopped accepting new customers on 30 April 2026, so the
> deployment workflow in `.github/workflows/deploy-api.yml` cannot be used on a
> new account at all. Running the API locally against real Cognito and real S3
> is the path that still works.

## 1. Set a budget alarm before creating anything

AWS Console → Billing and Cost Management → Budgets → Create budget → Zero
spend budget. Enter your email. This mails you the moment the account is billed
anything at all.

## 2. Apply

```bash
cd infra/terraform/freetier
cat > terraform.tfvars <<'EOF'
aws_region = "us-east-1"
EOF

terraform init
terraform plan      # read it
terraform apply
```

`terraform.tfvars` is gitignored. Nothing here writes a secret to disk.

## 3. Create a user

Self-signup would email a verification code. Creating the user as an
administrator skips that:

```bash
POOL=$(terraform output -raw cognito_user_pool_id)

aws cognito-idp admin-create-user \
    --user-pool-id "$POOL" --username you@example.com --message-action SUPPRESS
aws cognito-idp admin-set-user-password \
    --user-pool-id "$POOL" --username you@example.com \
    --password 'ChangeThis!2026' --permanent
aws cognito-idp admin-add-user-to-group \
    --user-pool-id "$POOL" --username you@example.com --group-name admin
```

## 4. Point the local API at real AWS

```bash
terraform output -raw api_env >> ../../../backend/.env
```

Then start the stack as usual (`docker compose up -d`) and restart the API so
it picks the new values up.

## 5. Prove it end to end

```bash
pip install boto3 requests
set -a && source ../../../backend/.env && set +a   # verify.py reads these too
python verify.py --email you@example.com --password 'ChangeThis!2026'
```

It signs in to Cognito, prints the issuer and claims off the returned token,
sends that token to the local API, uploads a file through the presigned URL the
API returns, confirms the object landed in S3, and then waits for the validator
Lambda to tag it. Every step touches real AWS, and the last one proves the S3
event actually reached the function.

Screenshot the output, plus the Cognito pool, the S3 object's tags, and the
function's CloudWatch log line — that is the evidence the integration works.

To see a rejection, upload something whose bytes disagree with the declared
type; the object comes back tagged `validation=failed` and the API refuses to
attach it.

## 6. Exercise the supplier price webhook

```bash
URL=$(terraform output -raw supplier_price_webhook_url)
SECRET=$(terraform output -raw webhook_secret)
BODY='{"supplier_id":"<a real supplier uuid>","sku":"<a real sku>","unit_cost":13.75,"currency":"CAD"}'
STAMP=$(date +%s)
SIG=$(printf '%s.%s' "$STAMP" "$BODY" | openssl dgst -sha256 -hmac "$SECRET" | awk '{print $2}')

curl -sS -X POST "$URL" \
    -H "content-type: application/json" \
    -H "x-stockroom-timestamp: $STAMP" \
    -H "x-stockroom-signature: v1=$SIG" \
    -d "$BODY"
```

A correct signature returns `202` with a submission id. Change one character of
the body without re-signing and it returns `401` — worth doing once, because
that is the control the endpoint rests on.

Then drain the inbox from the API (as an admin):

```bash
curl -sS -X POST localhost:8000/api/supplier-prices/ingest -H "Authorization: Bearer $TOKEN"
```

A quote more than the configured threshold above the item's last purchase price
comes back as `"status": "alert"` and shows up in `/api/audit-events` as
`supplier_price_alert`.

## 7. Destroy

```bash
terraform destroy
```

`force_destroy` is set on the bucket, so the test objects do not block it, and
the user pool has deletion protection off. Confirm afterwards in the console
that both are gone, and check Billing → Bills the next day.

## What this does and does not establish

It establishes that the Cognito verification and S3 presigning in this
repository work against real AWS, and that you have provisioned, configured and
torn down AWS resources with Terraform.

It does not make this a deployed product. The API ran on your machine; nothing
here served real users or stayed up. Describe it as what it is.
