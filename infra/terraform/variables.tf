variable "aws_region" {
  type = string
}

variable "project_name" {
  type    = string
  default = "stockroom"
}

variable "vpc_id" {
  type = string
}

variable "private_subnet_ids" {
  type = list(string)
}

variable "vpc_cidr" {
  type = string
}

variable "database_name" {
  type    = string
  default = "stockroom"
}

variable "database_username" {
  type    = string
  default = "stockroom_admin"
}

variable "frontend_callback_urls" {
  type    = list(string)
  default = ["http://localhost:3000"]
}

variable "frontend_origins" {
  type    = list(string)
  default = ["http://localhost:3000"]
}
