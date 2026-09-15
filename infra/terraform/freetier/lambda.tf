# The receipt validator: S3 ObjectCreated -> Lambda -> object tags.
#
# Stays inside the always-free Lambda allowance (1M requests and 400,000
# GB-seconds a month) at any volume this project will ever see. boto3 ships in
# the Python runtime, so the package is the handler and nothing else.

data "archive_file" "receipt_validator" {
  type        = "zip"
  source_dir  = "${path.module}/../../../backend/lambdas/receipt_validator"
  output_path = "${path.module}/.build/receipt_validator.zip"
  excludes    = ["__pycache__"]
}

resource "aws_iam_role" "receipt_validator" {
  name = "${local.name}-freetier-receipt-validator"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRole"
      Principal = { Service = "lambda.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy_attachment" "receipt_validator_logs" {
  role       = aws_iam_role.receipt_validator.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

# Read the object and write its verdict — nothing else, and only in this bucket.
resource "aws_iam_role_policy" "receipt_validator_bucket" {
  name = "receipt-validator-bucket-access"
  role = aws_iam_role.receipt_validator.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["s3:GetObject", "s3:GetObjectTagging", "s3:PutObjectTagging"]
      Resource = "${aws_s3_bucket.receipts.arn}/receipts/*"
    }]
  })
}

resource "aws_lambda_function" "receipt_validator" {
  function_name    = "${local.name}-freetier-receipt-validator"
  role             = aws_iam_role.receipt_validator.arn
  handler          = "handler.handler"
  runtime          = "python3.12"
  timeout          = 10
  memory_size      = 128
  filename         = data.archive_file.receipt_validator.output_path
  source_code_hash = data.archive_file.receipt_validator.output_base64sha256

  environment {
    variables = {
      MAX_RECEIPT_SIZE_BYTES = tostring(var.max_receipt_size_bytes)
    }
  }

  depends_on = [aws_iam_role_policy_attachment.receipt_validator_logs]
}

# Without an explicit retention the log group is created implicitly and kept
# forever, which is the one way this function could quietly start costing money.
resource "aws_cloudwatch_log_group" "receipt_validator" {
  name              = "/aws/lambda/${aws_lambda_function.receipt_validator.function_name}"
  retention_in_days = 7
}

resource "aws_lambda_permission" "allow_bucket" {
  statement_id  = "AllowExecutionFromS3Bucket"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.receipt_validator.arn
  principal     = "s3.amazonaws.com"
  source_arn    = aws_s3_bucket.receipts.arn
}

resource "aws_s3_bucket_notification" "receipts" {
  bucket = aws_s3_bucket.receipts.id

  lambda_function {
    lambda_function_arn = aws_lambda_function.receipt_validator.arn
    events              = ["s3:ObjectCreated:*"]
    filter_prefix       = "receipts/"
  }

  depends_on = [aws_lambda_permission.allow_bucket]
}
