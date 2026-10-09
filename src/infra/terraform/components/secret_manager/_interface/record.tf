# Normalizes canonical secret manager output records including secret ARNs, IDs, and KMS key identifiers.

locals {
  record = var.realized == null ? null : {
    secret_id   = var.realized.secret_id
    secret_arn  = var.realized.secret_arn
    secret_name = var.secret_name
    keys        = nonsensitive(keys(var.secret_values))
  }
}
