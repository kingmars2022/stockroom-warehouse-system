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

Those are exactly the two services the application code talks to. PostgreSQL
stays local in Docker — the API only sees a connection string, so running it on
RDS proves nothing the local database does not.

## Cost

Both services have free tiers this usage sits far inside: a handful of users
and a few kilobytes of objects. Everything that actually costs money — RDS, NAT
gateways, load balancers, App Runner — is excluded by design, not by luck.

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
API returns, and confirms the object landed in S3. Every step touches real AWS.

Screenshot the output, plus the Cognito pool and the S3 object in the console —
that is the evidence that the integration works.

## 6. Destroy

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
