locals {
  name = var.project_name
}

resource "aws_s3_bucket" "receipts" {
  bucket_prefix = "${local.name}-receipts-"
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
    allowed_methods = ["PUT"]
    allowed_origins = var.frontend_origins
    allowed_headers = ["content-type"]
    max_age_seconds = 300
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "receipts" {
  bucket = aws_s3_bucket.receipts.id

  rule {
    id     = "abort-incomplete-uploads"
    status = "Enabled"

    filter {}

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}

resource "aws_ecr_repository" "api" {
  name                 = "${local.name}-api"
  image_tag_mutability = "IMMUTABLE"
}

resource "aws_cognito_user_pool" "users" {
  name                     = "${local.name}-users"
  username_attributes      = ["email"]
  auto_verified_attributes = ["email"]
  mfa_configuration        = "OPTIONAL"

  admin_create_user_config {
    allow_admin_create_user_only = false
  }

  verification_message_template {
    default_email_option = "CONFIRM_WITH_CODE"
    email_subject        = "Confirm your Stockroom account"
    email_message        = "Your Stockroom verification code is {####}."
  }

  password_policy {
    minimum_length    = 12
    require_lowercase = true
    require_numbers   = true
    require_symbols   = true
    require_uppercase = true
  }
}

resource "aws_cognito_user_pool_client" "web" {
  name                                 = "${local.name}-web"
  user_pool_id                         = aws_cognito_user_pool.users.id
  generate_secret                      = false
  explicit_auth_flows                  = ["ALLOW_USER_SRP_AUTH", "ALLOW_REFRESH_TOKEN_AUTH", "ALLOW_USER_PASSWORD_AUTH"]
  allowed_oauth_flows_user_pool_client = true
  allowed_oauth_flows                  = ["code"]
  allowed_oauth_scopes                 = ["openid", "email", "profile"]
  callback_urls                        = var.frontend_callback_urls
  logout_urls                          = var.frontend_callback_urls
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

resource "aws_db_subnet_group" "main" {
  name       = "${local.name}-db"
  subnet_ids = var.private_subnet_ids
}

resource "aws_security_group" "database" {
  name        = "${local.name}-database"
  description = "PostgreSQL access from the application VPC"
  vpc_id      = var.vpc_id

  ingress {
    protocol    = "tcp"
    from_port   = 5432
    to_port     = 5432
    cidr_blocks = [var.vpc_cidr]
  }

  egress {
    protocol    = "-1"
    from_port   = 0
    to_port     = 0
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_db_instance" "postgres" {
  identifier                  = "${local.name}-postgres"
  engine                      = "postgres"
  instance_class              = "db.t4g.micro"
  allocated_storage           = 20
  max_allocated_storage       = 100
  db_name                     = var.database_name
  username                    = var.database_username
  manage_master_user_password = true
  db_subnet_group_name        = aws_db_subnet_group.main.name
  vpc_security_group_ids      = [aws_security_group.database.id]
  backup_retention_period     = 7
  deletion_protection         = true
  skip_final_snapshot         = false
}

resource "aws_sns_topic" "price_alerts" {
  name = "${local.name}-price-alerts"
}
