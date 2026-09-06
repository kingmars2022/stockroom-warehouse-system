output "receipt_bucket_name" {
  value = aws_s3_bucket.receipts.bucket
}

output "cognito_user_pool_id" {
  value = aws_cognito_user_pool.users.id
}

output "cognito_client_id" {
  value = aws_cognito_user_pool_client.web.id
}

output "cognito_issuer" {
  value = "https://cognito-idp.${var.aws_region}.amazonaws.com/${aws_cognito_user_pool.users.id}"
}

output "api_repository_url" {
  value = aws_ecr_repository.api.repository_url
}

output "database_secret_arn" {
  value = aws_db_instance.postgres.master_user_secret[0].secret_arn
}

output "price_alert_topic_arn" {
  value = aws_sns_topic.price_alerts.arn
}
