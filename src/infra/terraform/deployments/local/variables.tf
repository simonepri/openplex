# Declares input variables for local cluster endpoints, Kubernetes context names, and component toggles.

variable "floci_endpoint" {
  type        = string
  description = "Local Floci AWS emulator endpoint"
  default     = "http://127.0.0.1:4566"
}

variable "git_repo_url" {
  type        = string
  description = "Git repository for Argo CD sync"
  default     = "git://172.19.255.21:9418/openplex.git"
}

variable "git_identity_url" {
  type        = string
  description = "Local smart-HTTP repository URL used by Argo reference discovery and webhook matching."
  # LINT.IfChange(webhook-repository)
  default = "http://172.19.255.21:9419/cgi-bin/git/openplex.git"
  # LINT.ThenChange(//src/infra/tools/cloud_emulator/stack/compose.yaml:webhook-repository)
}

variable "fleet_availability" {
  type        = string
  description = "Fleet availability profile (standalone, replicated, or resilient)"
  default     = "standalone"
}
