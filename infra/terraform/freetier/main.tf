locals {
  name = var.project_name
}

# force_destroy lets `terraform destroy` remove the bucket with the test
# receipts still in it. The production stack deliberately omits this.
resource "aws_s3_bucket" "receipts" {
  bucket_prefix = "${local.name}-freetier-receipts-"
  force_destroy = true
}

resource "aws_s3_bucket_public_access_block" "receipts" {
  bucket                  = aws_s3_bucket.receipts.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "receipts" {
  bucket = aws_s3_bucket.receipts.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_cors_configuration" "receipts" {
  bucket = aws_s3_bucket.receipts.id

  cors_rule {
    allowed_methods = ["PUT", "GET"]
    allowed_origins = var.frontend_origins
    allowed_headers = ["content-type"]
    max_age_seconds = 300
  }
}

resource "aws_cognito_user_pool" "users" {
  name                     = "${local.name}-freetier-users"
  username_attributes      = ["email"]
  auto_verified_attributes = ["email"]
  deletion_protection      = "INACTIVE"

  password_policy {
    minimum_length    = 12
    require_lowercase = true
    require_numbers   = true
    require_symbols   = true
    require_uppercase = true
  }
}

resource "aws_cognito_user_pool_client" "web" {
  name                = "${local.name}-freetier-web"
  user_pool_id        = aws_cognito_user_pool.users.id
  generate_secret     = false
  explicit_auth_flows = ["ALLOW_USER_SRP_AUTH", "ALLOW_REFRESH_TOKEN_AUTH", "ALLOW_USER_PASSWORD_AUTH"]
}

resource "aws_cognito_user_group" "admin" {
  name         = "admin"
  user_pool_id = aws_cognito_user_pool.users.id
}

resource "aws_cognito_user_group" "supervisor" {
  name         = "supervisor"
  user_pool_id = aws_cognito_user_pool.users.id
}

resource "aws_cognito_user_group" "employee" {
  name         = "employee"
  user_pool_id = aws_cognito_user_pool.users.id
}
