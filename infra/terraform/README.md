# AWS foundation

This configuration creates the managed building blocks for a production Stockroom environment:

- Cognito user pool and `admin`, `supervisor`, and `employee` groups.
- Private S3 receipt bucket for invoices, purchase receipts, and reimbursement evidence.
- ECR repository for the FastAPI container.
- PostgreSQL RDS instance with automated backups and AWS-managed database credentials.
- SNS topic for price-increase notifications.

The AWS account VPC and private subnets are supplied as variables because network ownership differs by account. Configure App Runner to use the ECR repository and a VPC connector that can reach the private RDS database.

Before applying, create a `terraform.tfvars` file outside source control:

```hcl
aws_region         = "us-east-1"
vpc_id             = "vpc-..."
private_subnet_ids = ["subnet-...", "subnet-..."]
vpc_cidr           = "10.0.0.0/16"
```

Run `terraform init`, then `terraform plan`, and review the plan before applying it. The generated database credential secret must be supplied to App Runner as a runtime secret, never committed to the repository.
