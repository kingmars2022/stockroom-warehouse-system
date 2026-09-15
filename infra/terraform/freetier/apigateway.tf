# Public ingress for supplier price pushes: API Gateway HTTP API -> Lambda -> S3.
#
# An HTTP API rather than a REST API: it is the cheaper and simpler of the two,
# and none of what the REST flavour adds (request validators, usage plans, API
# keys) is wanted here — the signature is the authentication.
#
# Nothing in this file can reach the database. That is the point: a route
# exposed to the whole internet should not also hold a path to private data.

# The shared secret is generated here and passed to the function as an
# environment variable. Secrets Manager would be the right home for it in
# production, but it bills per secret per month, and this stack is meant to
# cost nothing — so the secret lives in Terraform state (already gitignored,
# already sensitive) and is read back with `terraform output`.
resource "random_password" "webhook_secret" {
  length  = 48
  special = false
}

data "archive_file" "supplier_price_webhook" {
  type        = "zip"
  source_dir  = "${path.module}/../../../backend/lambdas/supplier_price_webhook"
  output_path = "${path.module}/.build/supplier_price_webhook.zip"
  excludes    = ["__pycache__"]
}

resource "aws_iam_role" "supplier_price_webhook" {
  name = "${local.name}-freetier-price-webhook"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRole"
      Principal = { Service = "lambda.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy_attachment" "supplier_price_webhook_logs" {
  role       = aws_iam_role.supplier_price_webhook.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

# Write-only, and only into the submissions prefix. An internet-facing
# function has no reason to be able to read the receipts sitting next to it.
resource "aws_iam_role_policy" "supplier_price_webhook_bucket" {
  name = "price-webhook-bucket-access"
  role = aws_iam_role.supplier_price_webhook.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["s3:PutObject"]
      Resource = "${aws_s3_bucket.receipts.arn}/price-submissions/*"
    }]
  })
}

resource "aws_lambda_function" "supplier_price_webhook" {
  function_name    = "${local.name}-freetier-price-webhook"
  role             = aws_iam_role.supplier_price_webhook.arn
  handler          = "handler.handler"
  runtime          = "python3.12"
  timeout          = 10
  memory_size      = 128
  filename         = data.archive_file.supplier_price_webhook.output_path
  source_code_hash = data.archive_file.supplier_price_webhook.output_base64sha256

  environment {
    variables = {
      RECEIPT_BUCKET_NAME = aws_s3_bucket.receipts.bucket
      WEBHOOK_SECRET      = random_password.webhook_secret.result
    }
  }

  depends_on = [aws_iam_role_policy_attachment.supplier_price_webhook_logs]
}

resource "aws_cloudwatch_log_group" "supplier_price_webhook" {
  name              = "/aws/lambda/${aws_lambda_function.supplier_price_webhook.function_name}"
  retention_in_days = 7
}

resource "aws_apigatewayv2_api" "webhooks" {
  name          = "${local.name}-freetier-webhooks"
  protocol_type = "HTTP"
}

resource "aws_apigatewayv2_integration" "supplier_price_webhook" {
  api_id                 = aws_apigatewayv2_api.webhooks.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.supplier_price_webhook.invoke_arn
  payload_format_version = "2.0"
}

resource "aws_apigatewayv2_route" "supplier_price_webhook" {
  api_id    = aws_apigatewayv2_api.webhooks.id
  route_key = "POST /supplier-prices"
  target    = "integrations/${aws_apigatewayv2_integration.supplier_price_webhook.id}"
}

resource "aws_apigatewayv2_stage" "default" {
  api_id      = aws_apigatewayv2_api.webhooks.id
  name        = "$default"
  auto_deploy = true

  # A public endpoint with no throttle is a way to be billed by strangers.
  default_route_settings {
    throttling_rate_limit  = 10
    throttling_burst_limit = 20
  }
}

resource "aws_lambda_permission" "allow_apigateway" {
  statement_id  = "AllowExecutionFromAPIGateway"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.supplier_price_webhook.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.webhooks.execution_arn}/*/*"
}
