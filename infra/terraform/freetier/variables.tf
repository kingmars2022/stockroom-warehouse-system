variable "aws_region" {
  type = string
}

variable "project_name" {
  type    = string
  default = "stockroom"
}

variable "frontend_origins" {
  type    = list(string)
  default = ["http://localhost:3000"]
}

# Kept in step with max_receipt_size_bytes in the API settings: the validator
# enforces the same ceiling on the bytes that actually arrived.
variable "max_receipt_size_bytes" {
  type    = number
  default = 10485760
}
