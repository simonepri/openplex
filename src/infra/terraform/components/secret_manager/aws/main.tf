# Provisions AWS Secrets Manager secrets, customer-managed KMS encryption keys, and initial secret payloads.

module "interface" {
  source = "../_interface"

  secret_name   = var.secret_name
  secret_values = var.secret_values
  realized = {
    secret_id  = aws_secretsmanager_secret.secret.id
    secret_arn = aws_secretsmanager_secret.secret.arn
  }
}

resource "aws_kms_key" "secret" {
  description             = "Customer-managed key for ${var.secret_name} secret encryption"
  deletion_window_in_days = 7
  enable_key_rotation     = true
}

resource "aws_kms_alias" "secret" {
  name          = "alias/${var.kms_alias_prefix}${var.secret_name}"
  target_key_id = aws_kms_key.secret.key_id
}

resource "aws_secretsmanager_secret" "secret" {
  name                    = module.interface.names.secret
  kms_key_id              = aws_kms_key.secret.arn
  recovery_window_in_days = var.recovery_window_in_days
}

resource "aws_secretsmanager_secret_version" "secret" {
  secret_id     = aws_secretsmanager_secret.secret.id
  secret_string = jsonencode(var.secret_values)
}
