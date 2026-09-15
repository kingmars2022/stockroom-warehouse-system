output "receipt_bucket_name" {
  value = aws_s3_bucket.receipts.bucket
}

output "cognito_user_pool_id" {
  value = aws_cognito_user_pool.users.id
}

output "cognito_client_id" {
  value = aws_cognito_user_pool_client.web.id
}

output "receipt_validator_function_name" {
  value = aws_lambda_function.receipt_validator.function_name
}

output "supplier_price_webhook_url" {
  value = "${aws_apigatewayv2_api.webhooks.api_endpoint}/supplier-prices"
}

output "webhook_secret" {
  description = "Shared HMAC secret. Read with: terraform output -raw webhook_secret"
  value       = random_password.webhook_secret.result
  sensitive   = true
}

output "cognito_issuer" {
  value = "https://cognito-idp.${var.aws_region}.amazonaws.com/${aws_cognito_user_pool.users.id}"
}

output "api_env" {
  description = "Paste into backend/.env to point the local API at these real AWS resources."
  value       = <<-EOT
    COGNITO_REGION=${var.aws_region}
    COGNITO_USER_POOL_ID=${aws_cognito_user_pool.users.id}
    COGNITO_APP_CLIENT_ID=${aws_cognito_user_pool_client.web.id}
    RECEIPT_BUCKET_NAME=${aws_s3_bucket.receipts.bucket}
    AWS_REGION=${var.aws_region}
  EOT
}
