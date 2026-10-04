# Declares input variables for the dedicated DNS stack.

variable "enable_cloudflare" {
  description = "Controls whether Cloudflare DNS delegation records are managed. Set to false for local/Floci environments."
  type        = bool
  default     = null
}

variable "cloudflare_api_token" {
  description = "Cloudflare API token with DNS write permissions. Falls back to CLOUDFLARE_API_TOKEN environment variable."
  type        = string
  default     = null
  sensitive   = true
}

variable "zone_name" {
  description = "Apex domain in Cloudflare."
  type        = string
  default     = null
}

variable "delegations" {
  description = "Map of subdomain names to lists of delegated nameserver hostnames."
  type        = map(list(string))
  default     = null
}

variable "hooks_nlb_hostname" {
  description = "Hostname of the AWS Network Load Balancer routing GitHub webhooks to public-hooks gateway."
  type        = string
  default     = null
}
