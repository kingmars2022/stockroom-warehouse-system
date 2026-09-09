# Stockroom

**Stockroom is a warehouse operations system for courier, logistics, and internal supply teams.** It connects barcode-assisted stock issues, real-time inventory updates, replenishment forecasting, supplier comparison, receipt-backed purchasing, reimbursement approvals, and audit history in one operational workspace.

The product is designed for the day-to-day reality of a parcel or operations warehouse: employees need to issue materials quickly, supervisors need to prevent stockouts without over-ordering, and administrators need a defensible record of every financial and inventory decision.

## Contents

- [Business Context](#business-context)
- [Users and Responsibilities](#users-and-responsibilities)
- [End-to-End Operational Workflows](#end-to-end-operational-workflows)
- [Implemented Capabilities](#implemented-capabilities)
- [Architecture](#architecture)
- [Why This Architecture](#why-this-architecture)
- [Inventory, Forecasting, and Supplier Decision Logic](#inventory-forecasting-and-supplier-decision-logic)
- [Data Model](#data-model)
- [Security and Authorization](#security-and-authorization)
- [API Reference](#api-reference)
- [Run Locally](#run-locally)
- [AWS Deployment](#aws-deployment)
- [Testing and Quality Checks](#testing-and-quality-checks)
- [Project Structure](#project-structure)
- [Operational Metrics](#operational-metrics)
- [Troubleshooting](#troubleshooting)
- [Current Scope and Roadmap](#current-scope-and-roadmap)

---

## Business Context

Warehouse teams frequently operate across spreadsheets, chat threads, paper receipts, and informal handoffs. That creates several operational risks:

- Staff take packaging, labels, cables, printers, or office supplies without a consistent record of who took them or why.
- Inventory is discovered to be low only after a team cannot complete a shipment, packing run, or workstation setup.
- Procurement teams see a supplier price increase too late and cannot compare current options using price, lead time, and supplier reliability.
- Employees who make urgent purchases cannot easily connect a receipt, reimbursement request, approval decision, and payment status.
- Managers cannot reliably reconstruct what happened during a stock discrepancy, purchasing review, or financial audit.

Stockroom converts those disconnected activities into an accountable workflow. It is intentionally focused on small and mid-sized operations rather than attempting to replace a full enterprise ERP.

### Product Goals

1. Make stock movement traceable at the moment it happens.
2. Prevent invalid issues that would make stock negative.
3. Turn recent outbound demand into an explainable replenishment signal.
4. Give supervisors supplier choices based on recent price, lead time, rating, and preferred status.
5. Keep receipts, reimbursement approvals, and payment decisions attached to their business records.
6. Give administrators a searchable and exportable audit trail.

---

## Users and Responsibilities

| Role | Primary responsibility | What the role can see | What the role can do |
| --- | --- | --- | --- |
| **Employee** | Issue materials and submit reimbursement requests | Current item balances, the employee's own stock issues, and the employee's own expenses | Scan or select an item, issue stock, submit a receipt-backed expense, and track the request status |
| **Supervisor** | Run warehouse operations and purchasing | Warehouse activity, supplier data, purchase history, replenishment recommendations, and all expenses | Receive purchases, compare supplier options, record stock issues, and approve or reject submitted expenses |
| **Administrator** | Control policy, financial completion, and auditability | All warehouse, procurement, reimbursement, and audit data | Create inventory items, update the price-alert policy, approve/pay expenses, export audit history, and configure company display defaults |

### External System Interactions

| System | Interaction |
| --- | --- |
| **Amazon Cognito** | Authenticates users, verifies email addresses, manages password recovery, and supplies group claims for `admin`, `supervisor`, and `employee` roles |
| **FastAPI** | Validates Cognito JWTs, enforces authorization, runs inventory transactions, calculates replenishment recommendations, and creates presigned attachment URLs |
| **PostgreSQL** | Stores the operational source of truth for inventory, movements, purchases, expenses, suppliers, users, and audit logs |
| **Amazon S3** | Stores private receipts and invoices; the browser uploads through short-lived presigned URLs rather than through public file storage |
| **Amazon ECR / App Runner** | Packages and runs the FastAPI service in the AWS deployment design |
| **GitHub Actions** | Builds the frontend, runs API tests, and contains the API image deployment workflow |

---

## End-to-End Operational Workflows

### 1. Employee barcode-assisted stock issue

**Actor:** Employee, supervisor, or administrator

1. The user opens **Issue stock**.
2. The user can select an item manually or choose **Scan item barcode**.
3. The browser requests camera permission and scans a QR code or barcode whose decoded value matches an item SKU, such as `PK-LBL-013`.
4. Stockroom automatically selects the matching item. The user enters quantity, recipient/team, and a business note.
5. The frontend submits a `POST /api/movements` request with the current Cognito ID token.
6. FastAPI validates the token and role. The inventory service locks the item row, verifies that sufficient stock exists, reduces the on-hand quantity, creates a stock movement, and writes an audit event in one database transaction.
7. The user immediately sees the new balance and activity record. An employee sees only their own issue records; a supervisor sees warehouse activity; an administrator can see the audit trail.

**Result:** Stock changes immediately, the system cannot create a negative balance, and the action is attributable to a person and recipient.

### 2. Replenishment forecast

**Actors:** Supervisor and administrator

1. The API reviews outbound stock movements from the most recent 90-day observation window.
2. For each attention item, it calculates daily consumption, days of stock cover, and a suggested replenishment quantity.
3. The dashboard exposes the highest-risk items in **Replenishment forecast**.
4. The procurement page displays a recommended purchase plan with the current on-hand quantity, suggested order quantity, days of cover, and ranked supplier choices.

**Result:** A supervisor can act before a shortage blocks dispatch, packaging, or maintenance work.

### 3. Supplier selection and purchase receipt

**Actors:** Supervisor and administrator

1. A supervisor opens **Procurement** and reviews recommended purchase items.
2. For an item with purchase history, Stockroom lists supplier alternatives using each supplier's latest recorded unit price, lead time, rating, and preferred status.
3. The highest-ranked option pre-fills the supplier, suggested quantity, and latest unit cost in the purchase form. The supervisor can still override the recommendation.
4. The supervisor enters the final quantity, invoice or purchase-order number, and receipt file.
5. The browser requests a presigned S3 upload URL, uploads the receipt directly to a private bucket, then submits the purchase to the API.
6. The API increases on-hand inventory, creates an inbound movement, records the purchase, calculates price change versus the previous purchase of the same item, and writes an audit event.
7. If the price change crosses the configured threshold, the purchase appears in the supplier review queue.

**Result:** Procurement decisions remain human-controlled but are supported by current operational evidence.

### 4. Receipt-backed reimbursement approval

**Actors:** Employee, supervisor, administrator

1. An employee submits an expense with supplier, item, quantity, amount, business purpose, and receipt attachment.
2. The employee can only see their own reimbursement records.
3. A supervisor or administrator can approve or reject a `submitted` expense.
4. Only an administrator can mark an `approved` expense as `paid`.
5. Each submission, approval, rejection, and payment state transition creates an audit log entry with actor, role, timestamp, target, and detail.

**Result:** Receipt, approval, and payment evidence remain connected instead of being distributed across email, chat, and spreadsheets.

### 5. Administrator review and audit export

**Actor:** Administrator

1. The administrator reviews low-stock items, price alerts, reimbursement state, and supplier activity.
2. The administrator opens **Audit log** to inspect who performed an action, the actor's role, the target record, and the time of the action.
3. The administrator can export audit entries as CSV for operations, finance, or compliance review.

---

## Implemented Capabilities

### Authentication and role-based access

- Cognito email/password sign-in through AWS Amplify.
- Employee self-registration, email confirmation, and password recovery flows when Cognito is configured.
- New self-registered Cognito users resolve to the least-privileged `employee` role unless an administrator assigns a Cognito group.
- Server-side JWT signature, issuer, client, and token-use checks in FastAPI.
- Server-side role checks. Hiding an action in the user interface is not the only authorization control.

### Inventory operations

- Inventory catalog with SKU, category, location, unit, on-hand quantity, and minimum quantity.
- Search by item name, SKU, category, or location.
- Barcode or QR-code assisted item selection using the device camera through `html5-qrcode`.
- Manual item selection fallback when a camera is unavailable.
- Inbound and outbound stock movements with actor, recipient/source, note, and timestamp.
- Transactional negative-stock prevention.
- Employee-level activity filtering for outbound movements.

### Procurement and supplier intelligence

- Supplier directory with status, lead time, and rating.
- Purchase receipt recording with quantity, unit cost, currency, invoice reference, and attachment.
- Automatic on-hand quantity increase when a purchase is received.
- Purchase-price change calculation against the previous purchase for the same item.
- Configurable price-increase threshold.
- 90-day demand-based replenishment forecast.
- Supplier ranking based on latest item price, lead time, rating, and preferred status.
- Human override of the recommended supplier, order quantity, and price.

### Receipts, expenses, and auditability

- Private receipt uploads for PDF, JPEG, PNG, and WebP files.
- Maximum attachment size enforcement, defaulting to 10 MB.
- S3 presigned upload and download URLs with authorization checks before download.
- Expense state machine: `submitted` -> `approved` or `rejected`; `approved` -> `paid`.
- Complete audit records for inventory, procurement, expense, policy, and relevant administrative actions.
- CSV audit export for administrators.

### User experience

- Responsive operational dashboard.
- English, French, and Simplified Chinese login/navigation and appearance settings. Some detailed business-table terms remain English.
- Light, dark, and system theme modes.
- Green, blue, and orange accent choices and comfortable/compact information density.

---

## Architecture

### Logical Architecture

```mermaid
flowchart LR
    U[Warehouse user] --> W[Next.js / React frontend]
    W -->|Sign in, session, ID token| C[Amazon Cognito]
    W -->|Bearer ID token| A[FastAPI API]
    A -->|JWT validation and role checks| C
    A -->|Transactions and queries| P[(PostgreSQL)]
    A -->|Presigned upload/download URLs| S[Private Amazon S3 bucket]
    A --> L[Audit logs]
    G[GitHub Actions] -->|Build and test| W
    G -->|Build API image| E[Amazon ECR]
    E --> R[AWS App Runner]
    R --> A
```

### Production Deployment Topology

```text
Browser
  |
  +-- Next.js frontend on AWS Amplify
  |      |
  |      +-- Amazon Cognito user pool and role groups
  |
  +-- FastAPI container on AWS App Runner
         |
         +-- Amazon RDS for PostgreSQL
         +-- Private Amazon S3 receipt bucket
         +-- Amazon SNS price-alert topic
         +-- Amazon ECR image repository
```

The repository contains the AWS infrastructure definitions and deployment workflow. Provisioning an AWS account, networking, domains, runtime secrets, and App Runner service configuration remains an environment-specific deployment step.

### Request Lifecycle

1. Cognito authenticates the user and the frontend receives an ID token.
2. The frontend sends the token as `Authorization: Bearer <token>`.
3. FastAPI fetches the Cognito signing key, validates the token, maps the Cognito group to an application role, and finds or creates the internal user record.
4. The relevant service validates business rules and runs database work inside a transaction.
5. The service persists the domain record and a matching audit record before committing.
6. The frontend reloads current API state and renders it according to the user's role.

---

## Why This Architecture

| Decision | Reasoning |
| --- | --- |
| **Next.js + React + TypeScript** | Provides a typed, interactive operational interface for role-specific dashboards, dialogs, uploads, camera access, and responsive use on desktop or warehouse devices. |
| **FastAPI** | Keeps authorization and inventory rules on the server, provides strongly typed request validation, and exposes automatic OpenAPI documentation at `/docs`. |
| **PostgreSQL + SQLAlchemy + Alembic** | Inventory and financial workflows require transactions, constraints, durable history, and migrations. PostgreSQL is the operational source of truth rather than browser storage. |
| **Row locking for stock issue** | Concurrent warehouse users must not be able to issue the same remaining stock twice. The service locks the item row before verifying availability and updating quantity. |
| **Amazon Cognito + Amplify Auth** | Separates identity and password handling from the business API, supports email verification and password recovery, and provides signed claims for backend authorization. |
| **Amazon S3 presigned URLs** | Receipt files do not pass through public storage or browser local storage. A short-lived URL restricts upload/download access while keeping the API in control of authorization. |
| **`html5-qrcode`** | Uses a maintained browser-based scanner instead of hand-rolled image decoding. The scanner converts a camera result to an existing SKU, then the normal inventory transaction still enforces all business rules. |
| **Docker Compose** | Starts PostgreSQL and FastAPI with predictable local networking and a repeatable development environment. |
| **Terraform** | Defines Cognito, S3, ECR, RDS, and SNS infrastructure as reviewable and reproducible code. |
| **GitHub Actions** | Makes frontend build and API workflow tests part of pull-request and main-branch validation. |

---

## Inventory, Forecasting, and Supplier Decision Logic

### Inventory transaction guarantees

For an outbound movement, the API performs the following work in one transaction:

1. Lock the selected `items` row.
2. Verify the item exists.
3. Verify `quantity_on_hand >= requested_quantity`.
4. Decrement the current on-hand quantity.
5. Create a `stock_movements` record.
6. Create an `audit_logs` record.
7. Commit all changes together, or roll them all back on failure.

This prevents negative inventory and preserves a traceable movement ledger.

### Replenishment calculation

The current implementation is deliberately explainable rather than a black-box forecasting model.

1. Read outbound movements from the last 90 days.
2. Calculate daily usage from observed outbound quantity and the active demand period. If an item is already below minimum stock but has no recent outbound history, use a conservative fallback based on its minimum quantity.
3. Calculate days of cover as:

```text
days_of_cover = quantity_on_hand / daily_usage
```

4. Flag an item when it is already at/below its minimum quantity or is likely to run out within the supplier safety window.
5. Calculate target stock using the larger of two periods: two minimum-stock levels or demand across at least 14 days and the longest available supplier lead time plus a seven-day buffer.
6. Recommend the positive difference between target stock and current stock.

### Supplier ranking

For each supplier that has a recent purchase record for the item, Stockroom uses:

- Latest recorded unit cost for that item.
- Supplier lead time in days.
- Supplier rating out of five.
- Preferred supplier status.

The current score weights cost most heavily, then lead time, then rating, with a small preferred-supplier bonus. The highest score is preselected as a recommendation; it is never an automatic purchase decision. A supervisor or administrator can select any active supplier and alter quantity or cost before recording the receipt.

---

## Data Model

| Table | Purpose | Important fields |
| --- | --- | --- |
| `users` | Internal user profile linked to Cognito identity | `cognito_sub`, email, name, role, active |
| `items` | Inventory master data and current balance | SKU, name, category, location, unit, `quantity_on_hand`, `minimum_quantity` |
| `stock_movements` | Immutable operational movement history | item, inbound/outbound kind, quantity, actor, recipient, note, created time |
| `suppliers` | Supplier reference data | name, contact, `lead_days`, rating, preferred/backup/paused status |
| `purchases` | Procurement cost and receipt history | item, supplier, receiver, quantity, unit cost, invoice, receipt key, price-change percentage |
| `expenses` | Employee reimbursement records | submitter, item, supplier, amount, receipt key, status, reviewer |
| `audit_logs` | Administrative and operational audit evidence | actor, role, action, target type/id, detail, created time |
| `system_settings` | Business-policy settings | price-alert threshold and the user who last changed it |

### Relationship Summary

```text
User 1---* StockMovement *---1 Item
User 1---* Purchase      *---1 Item
Supplier 1---* Purchase
User 1---* Expense       *---0..1 Item
User 1---* AuditLog
```

---

## Security and Authorization

### Authorization model

- Cognito groups `admin`, `supervisor`, and `employee` drive the backend role.
- A new self-registered user with no elevated group resolves to `employee`.
- Supervisors and administrators must be assigned by an administrator through Cognito group management.
- FastAPI checks role permissions on every protected operation; the frontend is not the security boundary.

### Data protections

- Cognito JWT signature, issuer, client ID, and token-use checks run on the API.
- Receipt upload keys are scoped to the current user.
- Purchase receipts cannot be downloaded by employees.
- Employees can download only their own expense receipts.
- S3 objects remain private; the API generates short-lived URLs after authorization succeeds.
- The configured receipt policy accepts only PDF, JPEG, PNG, and WebP files and enforces a maximum file size.
- Inventory changes are transactional and audited.

### Secret handling

Never commit AWS credentials, database credentials, Cognito values, or production URLs to source control. Use AWS Secrets Manager or App Runner runtime secrets for production, and GitHub Secrets/Variables for CI/CD configuration.

---

## API Reference

FastAPI exposes interactive API documentation when the API is running:

```text
http://localhost:8000/docs
```

| Method | Endpoint | Roles | Purpose |
| --- | --- | --- | --- |
| `GET` | `/health` | Public | Health check |
| `GET` | `/api/me` | Any authenticated user | Current authenticated profile |
| `GET` | `/api/items` | Any authenticated user | Inventory catalog |
| `POST` | `/api/items` | Admin | Create inventory item |
| `GET` | `/api/movements` | Any authenticated user | Movements; employee scope is limited to their own actions |
| `POST` | `/api/movements` | Any authenticated user | Record inbound/outbound stock movement; employees cannot receive stock |
| `GET` | `/api/suppliers` | Admin, supervisor | Supplier list |
| `POST` | `/api/suppliers` | Admin | Create supplier |
| `GET` | `/api/purchases` | Admin, supervisor | Purchase history |
| `POST` | `/api/purchases` | Admin, supervisor | Receive purchase and update stock |
| `GET` | `/api/price-policy` | Admin, supervisor | Read price-increase threshold |
| `PATCH` | `/api/price-policy` | Admin | Update price-increase threshold |
| `GET` | `/api/replenishment-recommendations` | Admin, supervisor | Demand-based replenishment and supplier suggestions |
| `GET` | `/api/expenses` | Any authenticated user | Expenses; employee scope is limited to their own submissions |
| `POST` | `/api/expenses` | Any authenticated user | Submit expense |
| `PATCH` | `/api/expenses/{expense_id}/status` | Supervisor/admin depending on transition | Approve, reject, or pay expense |
| `GET` | `/api/audit-logs` | Admin | Full audit history |
| `POST` | `/api/attachments/presign` | Any authenticated user | Request private S3 receipt upload URL |
| `GET` | `/api/attachments/download` | Authorized user | Request authorized private receipt download URL |

### Common API responses

| Status | Meaning |
| --- | --- |
| `401` | Missing, expired, invalid, or incorrect Cognito token |
| `403` | Authenticated user does not have the required role or attachment access |
| `404` | Item, supplier, expense, or receipt record does not exist |
| `409` | Stock issue would make inventory negative, or SKU already exists |
| `413` | Receipt exceeds the configured maximum file size |
| `415` | Receipt file type is not accepted |
| `503` | Cognito or receipt storage is not configured |

---

## Run Locally

### Prerequisites

- Node.js `20.9.0` or later. Node 20 LTS or newer is recommended.
- npm.
- Docker Desktop for the PostgreSQL + FastAPI stack.
- Python 3.12 for API test execution, matching CI.
- An AWS account only when running the real Cognito/S3 workflow or deploying infrastructure.

### Option A: Run the local interactive demo

This is the fastest way to explore the product, role-specific pages, barcode workflow, forecast, supplier suggestions, expenses, and audit screens. It does not require Docker or AWS.

```bash
cd /Users/siguangzhao/Documents/GitHub/my-projects/projects/Stockroom-system
npm install
npm run dev -- --port 3001
```

Open [http://localhost:3001](http://localhost:3001).

When both Cognito values in `.env.local` are empty and the application is started in development mode, Stockroom enables a **local demo mode**. Demo data is held in browser memory and resets after a page refresh. It is intentionally isolated from the PostgreSQL, S3, and AWS authentication workflow.

| Role | Email | Password |
| --- | --- | --- |
| Administrator | `admin@stockroom.test` | `Stockroom!2026` |
| Supervisor | `supervisor@stockroom.test` | `Stockroom!2026` |
| Employee | `employee@stockroom.test` | `Stockroom!2026` |

### Test the barcode issue workflow

1. Start the local demo and sign in as `employee@stockroom.test`.
2. Prepare a QR code that contains an existing SKU, for example `PK-LBL-013`. A QR-code generator or a label printed for that SKU can be used.
3. Select **Issue stock** and choose **Scan item barcode**.
4. Allow the browser to use the camera and scan the code.
5. Confirm that the matching inventory item is selected, enter quantity and recipient, then submit.
6. Confirm the balance and the employee activity list change without refreshing the page.
7. Sign in as the administrator to review the audit record.

Camera access requires a secure browser context. `http://localhost` works for a laptop webcam. For phone-based testing, use a deployed HTTPS URL or an HTTPS development tunnel; most mobile browsers will not grant camera access to a plain `http://192.168.x.x` address.

### Option B: Run the real local API and database stack

This option starts PostgreSQL and FastAPI in Docker. It requires valid Cognito settings for authenticated API requests and a configured S3 bucket for receipt uploads.

```bash
cd /Users/siguangzhao/Documents/GitHub/my-projects/projects/Stockroom-system
cp .env.example .env.local
cp backend/.env.example backend/.env
docker compose up --build
```

In a second terminal, start the frontend:

```bash
cd /Users/siguangzhao/Documents/GitHub/my-projects/projects/Stockroom-system
npm install
npm run dev
```

The frontend defaults to `http://localhost:3000`, the API defaults to `http://localhost:8000`, and PostgreSQL is exposed on `localhost:5432`.

The API container runs Alembic migrations on startup. Check the running services with:

```bash
docker compose ps
curl http://localhost:8000/health
```

### Configure the real Cognito workflow

1. Deploy or identify an Amazon Cognito user pool and an app client without a client secret.
2. Set the frontend values in `.env.local`:

```dotenv
NEXT_PUBLIC_API_URL=http://localhost:8000
NEXT_PUBLIC_COGNITO_USER_POOL_ID=us-east-1_example
NEXT_PUBLIC_COGNITO_APP_CLIENT_ID=exampleclientid
```

3. Set matching API values in `backend/.env`:

```dotenv
APP_ENV=development
DATABASE_URL=postgresql+psycopg://stockroom:stockroom@db:5432/stockroom
CORS_ORIGINS=http://localhost:3000
AWS_REGION=us-east-1
COGNITO_REGION=us-east-1
COGNITO_USER_POOL_ID=us-east-1_example
COGNITO_APP_CLIENT_ID=exampleclientid
RECEIPT_BUCKET_NAME=company-stockroom-receipts
```

4. Restart both services after changing environment variables.
5. Create or confirm Cognito groups named `admin`, `supervisor`, and `employee`. Assign elevated users to the appropriate group.

Once Cognito variables are present, local demo authentication is disabled and the frontend uses the real Cognito sign-in, sign-up, verification, and password-reset workflow.

### Environment variable reference

| Variable | Used by | Required for | Description |
| --- | --- | --- | --- |
| `NEXT_PUBLIC_API_URL` | Frontend | Real API mode | Base URL for FastAPI |
| `NEXT_PUBLIC_COGNITO_USER_POOL_ID` | Frontend | Real authentication | Cognito user pool ID |
| `NEXT_PUBLIC_COGNITO_APP_CLIENT_ID` | Frontend | Real authentication | Cognito app client ID |
| `DATABASE_URL` | API | API startup | SQLAlchemy PostgreSQL connection string |
| `CORS_ORIGINS` | API | Browser/API integration | Comma-separated allowed frontend origins |
| `AWS_REGION` | API | S3 access | AWS region for S3 client |
| `COGNITO_REGION` | API | JWT validation | AWS region containing the user pool |
| `COGNITO_USER_POOL_ID` | API | JWT validation | Cognito user pool ID |
| `COGNITO_APP_CLIENT_ID` | API | JWT validation | Cognito app client ID |
| `RECEIPT_BUCKET_NAME` | API | Receipt upload/download | Private S3 bucket name |
| `MAX_RECEIPT_SIZE_BYTES` | API | Receipt validation | Maximum allowed attachment size; defaults to `10485760` |

---

## AWS Deployment

### Infrastructure as code

`infra/terraform` defines the managed components for a production environment:

- Cognito user pool, app client, email verification, and `admin` / `supervisor` / `employee` groups.
- Private, encrypted S3 receipt bucket with upload CORS and incomplete-upload lifecycle cleanup.
- ECR repository for the FastAPI image.
- PostgreSQL RDS instance with automated backups and managed database credentials.
- SNS topic for price-increase notifications.

The existing VPC and private subnets are inputs because network ownership and security policy are organization-specific.

Create an untracked `infra/terraform/terraform.tfvars` file:

```hcl
aws_region             = "us-east-1"
vpc_id                 = "vpc-..."
private_subnet_ids     = ["subnet-...", "subnet-..."]
vpc_cidr               = "10.0.0.0/16"
frontend_callback_urls = ["https://stockroom.example.com"]
frontend_origins       = ["https://stockroom.example.com"]
```

Review infrastructure before creation:

```bash
cd infra/terraform
terraform init
terraform plan
terraform apply
```

After apply, use Terraform outputs to populate the frontend and API environment variables:

```bash
terraform output cognito_user_pool_id
terraform output cognito_client_id
terraform output receipt_bucket_name
terraform output database_secret_arn
```

### API container deployment

The repository contains `.github/workflows/deploy-api.yml`.

When a change under `backend/` is pushed to `main`, the workflow:

1. Assumes an AWS role through GitHub OIDC.
2. Builds the FastAPI container.
3. Pushes the image to the `stockroom-api` ECR repository.
4. Requests a new deployment from the configured AWS App Runner service.

Configure these GitHub values before enabling a production deployment:

| Name | Type | Purpose |
| --- | --- | --- |
| `AWS_DEPLOY_ROLE_ARN` | GitHub Secret | OIDC-assumable AWS deployment role |
| `APP_RUNNER_SERVICE_ARN` | GitHub Secret | Existing App Runner service to redeploy |
| `AWS_REGION` | GitHub Variable | Region used by the deployment workflow |

App Runner must receive the database credential as a runtime secret and must use a VPC connector that can reach the private RDS instance. The API must also receive its Cognito and S3 configuration as runtime environment values/secrets.

### Frontend deployment

`amplify.yml` supplies the AWS Amplify build configuration. Configure the Amplify application with the production frontend environment variables and production API URL. A deployed frontend must use HTTPS so mobile browser camera access works for barcode scanning.

---

## Testing and Quality Checks

### Automated tests

The API test suite covers critical business rules:

- An outbound movement cannot produce negative stock.
- An employee cannot receive stock.
- A purchase updates inventory and calculates a price change.
- Reimbursement approval requires a supervisor or administrator, and payment requires an administrator.
- Replenishment recommendations return an attention item and rank suppliers using price and lead time.

Run API tests locally (`pytest.ini` sets `pythonpath = .`, so run it from inside `backend/`, not the repo root — running it from the repo root breaks coverage measurement):

```bash
pip install -r backend/requirements-dev.txt
cd backend && pytest
```

The suite above runs on SQLite for speed and isolation, which means one thing it can't exercise is the `SELECT ... FOR UPDATE` row lock in `record_movement` -- SQLite has no row-level locking. `tests/test_concurrency.py` covers that: 20 threads race to issue stock for an item with 10 units on hand, and it self-skips unless `POSTGRES_TEST_URL` points at a real, disposable PostgreSQL database:

```bash
POSTGRES_TEST_URL=postgresql+psycopg://stockroom:stockroom@localhost:5432/stockroom_test \
    pytest tests/test_concurrency.py --no-cov
```

Run the production frontend build:

```bash
npm run build
```

### Load testing

`backend/loadtest/` runs the real application (not a mock) against a real,
disposable PostgreSQL + Redis, driven by concurrent virtual users over
actual HTTP. Measured with 50 concurrent users for 30 seconds on a single
unscaled `uvicorn` process: **163.5 req/s, 0 errors, p95 latency 272.7 ms**
-- see `backend/loadtest/README.md` for the full methodology, numbers, and
how to reproduce it.

### Continuous integration

`.github/workflows/ci.yml` runs for pull requests and pushes to `main`:

- **`frontend`** — `npm ci`, `npm run lint`, `npm run build`.
- **`api`** — installs Python 3.12 dependencies, then runs the suite three times: once with coverage (`pytest.ini` sets `--cov=app --cov-fail-under=80`, so the job fails if coverage regresses), once more against a live Redis service (`test_cache.py` only, so the cache path is exercised against a real server instead of only the in-memory fallback), and once more against a live PostgreSQL service (`test_concurrency.py` only, proving the row lock holds under real concurrency). The coverage XML is uploaded as a build artifact.
- **`manifests`** — validates everything under `infra/k8s/` with [kubeconform](https://github.com/yannh/kubeconform).

### Manual acceptance checklist

1. Log in as an employee and issue stock using a scanned SKU.
2. Confirm the balance falls and the employee can see the issue in their activity scope.
3. Log in as a supervisor and verify the replenishment forecast and supplier recommendation are visible.
4. Record a purchase with a receipt and confirm stock increases and price history changes.
5. Submit an expense as an employee, approve it as a supervisor, and mark it paid as an administrator.
6. Log in as an administrator and export the corresponding audit entries.

---

## Project Structure

```text
.
├── app/
│   ├── page.tsx                 # Dashboard, role-based workflows, scanner UI, local demo mode
│   ├── lib/auth.ts              # AWS Amplify/Cognito authentication helpers
│   ├── lib/api.ts               # Authenticated API and receipt-upload client
│   ├── i18n.ts                  # English, French, and Simplified Chinese dictionaries
│   └── globals.css              # Responsive operational UI styles
├── backend/
│   ├── app/
│   │   ├── main.py              # FastAPI routes
│   │   ├── auth.py              # Cognito JWT validation and role dependencies
│   │   ├── models.py            # SQLAlchemy domain models
│   │   ├── schemas.py           # Pydantic request/response contracts
│   │   └── services.py          # Transactions, audit writes, forecasting, business rules
│   ├── alembic/                 # Database migration configuration and revisions
│   ├── tests/                   # API workflow tests
│   ├── Dockerfile               # FastAPI container image
│   └── start.sh                 # Migration then API startup command
├── infra/terraform/             # AWS infrastructure definitions
├── .github/workflows/           # CI validation and API deployment workflow
├── docker-compose.yml           # PostgreSQL + FastAPI local stack
├── amplify.yml                  # AWS Amplify frontend build settings
└── README.md
```

---

## Operational Metrics

Stockroom is designed to make measurable operational improvements. Do not claim a percentage improvement until a real baseline has been measured. Instead, capture the following before and after rollout:

| Workflow | Baseline to record | Evidence after rollout |
| --- | --- | --- |
| Inventory lookup | Median time to find an item and its location | Search and item-view workflow timing |
| Stock issue accountability | Percentage of issues with a known recipient and actor | `stock_movements` completeness |
| Stockout prevention | Number of urgent purchases or delayed packing runs | Replenishment queue and purchase history |
| Procurement review | Price increases reviewed before purchase approval | Purchase price history and supplier recommendations |
| Reimbursement processing | Time from submission to payment | `expenses` status timestamps and audit logs |
| Audit preparation | Time to prepare a monthly activity report | Exported audit-log workflow |

Calculate a measured time reduction as:

```text
(baseline_time - post_rollout_time) / baseline_time
```

For supplier savings, compare the chosen purchase cost against the lowest viable alternative available at the decision time, while retaining lead-time and quality constraints.

---

## Troubleshooting

### The login page says Cognito is not configured

For the local demo, leave both Cognito frontend variables empty and use the displayed `.test` accounts. For real authentication, populate both Cognito frontend values and matching backend values, then restart the frontend and API.

### The phone cannot open the camera scanner

Mobile browsers generally require HTTPS for camera access. Use an HTTPS deployment or tunnel for phone testing. `localhost` is treated as secure for a laptop browser, but a laptop LAN IP address normally is not.

### A scanned code does not select an item

The decoded QR/barcode value must exactly match the item SKU, ignoring letter case. Check the SKU in the inventory table and regenerate the code with that exact value.

### Receipt upload returns `503`

Set `RECEIPT_BUCKET_NAME`, `AWS_REGION`, and valid AWS runtime credentials for the API. The local demo does not use S3 uploads.

### The frontend cannot call the API

Confirm that `NEXT_PUBLIC_API_URL` points to the API, the API is healthy at `/health`, and `CORS_ORIGINS` includes the frontend origin. Restart both services after changing environment variables.

### Docker cannot start

Start Docker Desktop and wait for its daemon to become ready, then rerun:

```bash
docker compose up --build
```

---

## Current Scope and Roadmap

### Included now

- Barcode/QR-assisted stock issue with manual fallback.
- Transactional inventory movements and negative-stock protection.
- Demand-based replenishment forecast and supplier ranking.
- Purchase price history and threshold-based price review.
- Receipt-backed purchases and reimbursements.
- Role-aware approval, payment, and audit flows.
- Cognito, S3, PostgreSQL, Docker, Terraform, and CI/CD implementation paths.

### Planned extensions

- Cycle counts, variance investigation, and count approval workflows.
- Multiple warehouses, transfer orders, in-transit stock, and receiving confirmation.
- Batch/lot, serial number, and expiry-date tracking.
- Email, Slack, or Teams notifications for low stock, price changes, and pending approvals.
- Receipt OCR with human confirmation and correction history.
- Offline scan queue with conflict-aware synchronization for warehouse devices.
- Virus scanning, retention policies, and compliance archival for attachments.
- Full translation coverage for detailed business tables and messages.

---

## Suggested Product Demonstration

Use this sequence to demonstrate the complete system in a few minutes:

1. Sign in as an employee and scan a label SKU to issue stock.
2. Show the immediate balance change and employee-scoped activity record.
3. Sign in as a supervisor and open the replenishment forecast.
4. Open procurement, compare the recommended supplier against alternatives, and receive a purchase with a receipt.
5. Submit an expense, approve it as a supervisor, then mark it paid as an administrator.
6. Open the administrator audit log and export the complete decision trail.

This demonstrates the core Stockroom promise: a warehouse action begins with a physical item movement and ends with an accountable, searchable operational record.
