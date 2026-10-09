# Declares administrator-supplied input variables configuring cell domains, base images, and the workspaces namespace.

variable "cell" {
  description = "Cell whose workspaces namespace, storage gateway, and backup lineage this template uses."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,61}[a-z0-9]$", var.cell))
    error_message = "cell must be a lowercase DNS label."
  }
}

variable "access_alias_domain" {
  description = "Private split-DNS suffix in the form c.<domain>."
  type        = string

  validation {
    condition = (
      can(regex("^(?:c|k8s)\\.[a-z0-9](?:[-a-z0-9.]*[a-z0-9])$", var.access_alias_domain)) &&
      !endswith(var.access_alias_domain, ".localhost")
    )
    error_message = "access_alias_domain must be a lowercase c.* or k8s.* DNS suffix outside the browser-only .localhost namespace."
  }
}

# tflint-ignore: terraform_unused_declarations
variable "deployment_domain" {
  description = "Public DNS suffix for browser-facing deployment endpoints."
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?(?:\\.[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?)+$", var.deployment_domain))
    error_message = "deployment_domain must be a lowercase DNS suffix."
  }
}

variable "coder_app_domain" {
  description = "Coder wildcard domain without leading wildcard used for external workspace application and SSH routing."
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?(?:\\.[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?)+$", var.coder_app_domain))
    error_message = "coder_app_domain must be a lowercase DNS suffix."
  }
}

variable "headscale_url" {
  description = "Private access-plane enrollment and Headscale HTTPS endpoint."
  type        = string
  default     = ""

  validation {
    condition     = var.headscale_url == "" || can(regex("^https?://.+", var.headscale_url))
    error_message = "headscale_url must be empty or a valid HTTPS endpoint URL."
  }
}

variable "ca_config_map_name" {
  description = "Optional selected-cell CA ConfigMap included in the workspace trust bundle."
  type        = string
  default     = ""

  validation {
    condition     = var.ca_config_map_name == "" || var.ca_config_map_name == "cluster-local-ca"
    error_message = "ca_config_map_name must be empty or cluster-local-ca."
  }
}

variable "cell_ca_inventory" {
  description = "JSON object mapping each registered cell to the base64-encoded PEM CA of its kube-oidc-proxy, so workspaces can reach every cell."
  type        = string
  default     = "{}"

  validation {
    condition     = can(tomap(jsondecode(var.cell_ca_inventory))) && alltrue([for ca in values(jsondecode(var.cell_ca_inventory)) : can(base64decode(ca))])
    error_message = "cell_ca_inventory must be a JSON object of base64-encoded PEM CAs."
  }
}

variable "control_plane_ca_base64" {
  description = "Base64-encoded PEM CA for control-plane HTTPS endpoints used by workspace agents and sidecars."
  type        = string

  validation {
    condition = can(regex(
      "^-----BEGIN CERTIFICATE-----[A-Za-z0-9+/=\\r\\n]+-----END CERTIFICATE-----$",
      trimspace(base64decode(var.control_plane_ca_base64)),
    ))
    error_message = "control_plane_ca_base64 must encode one PEM certificate."
  }
}

variable "control_plane_name" {
  description = "Registered name of the control-plane cluster, whose kube-oidc-proxy every workspace kubeconfig includes."
  type        = string
  default     = ""
}

variable "repository_url" {
  description = "Administrator-owned Git repository cloned when the workspace first starts. Cloud publication requires HTTPS; the local emulator uses its read-only Git service."
  type        = string

  validation {
    condition = (
      can(regex("^(https|git)://[^[:space:]]+$", var.repository_url)) &&
      !can(regex("^(https|git)://[^/]*@", var.repository_url))
    )
    error_message = "repository_url must be an HTTPS or Git protocol URL without whitespace or embedded credentials."
  }
}

variable "checkout_path" {
  description = "Administrator-owned absolute mount and clone path for the persistent repository."
  type        = string
  default     = "/fs/depot"

  validation {
    condition     = can(regex("^/([A-Za-z0-9._-]+/)*[A-Za-z0-9._-]+$", var.checkout_path))
    error_message = "checkout_path must be an absolute path containing only letters, numbers, dots, underscores, and hyphens."
  }
}

variable "storage_class_name" {
  description = "RWO block StorageClass for workspace homes; it must delete its PV when the workspace PVC is deleted."
  type        = string

  validation {
    condition     = length(trimspace(var.storage_class_name)) > 0
    error_message = "storage_class_name must not be empty."
  }
}

variable "workload_registry" {
  description = "Administrator-owned OCI origin root; workload repository paths are appended to it."
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9]+(?:[.-][a-z0-9]+)*(?::[1-9][0-9]{0,4})?(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)*$", var.workload_registry))
    error_message = "workload_registry must be a registry host with an optional registry-safe path and no scheme or trailing slash."
  }
}

variable "workload_registry_insecure" {
  description = "Whether the declared workload origin uses HTTP; only the local Floci split-DNS endpoint may enable it."
  type        = string

  validation {
    condition     = contains(["false", "true"], var.workload_registry_insecure)
    error_message = "workload_registry_insecure must be false or true."
  }
}

variable "workload_origin_auth_mode" {
  description = "Credential path selected by the cell-owned workspace origin contract."
  type        = string

  validation {
    condition     = contains(["eks-pod-identity", "floci", "web-identity"], var.workload_origin_auth_mode)
    error_message = "workload_origin_auth_mode must be eks-pod-identity, floci, or web-identity."
  }
}

variable "workload_origin_region" {
  description = "AWS region containing the authoritative workload origin."
  type        = string

  validation {
    condition     = can(regex("^[a-z]{2}(?:-gov)?-[a-z]+-[0-9]+$", var.workload_origin_region))
    error_message = "workload_origin_region must be an AWS region."
  }
}

variable "workload_origin_provider" {
  description = "Cloud provider that owns the selected workspace cell's origin identity contract."
  type        = string

  validation {
    condition     = contains(["aws", "floci", "gcp"], var.workload_origin_provider)
    error_message = "workload_origin_provider must be aws, floci, or gcp."
  }
}

variable "workload_origin_role_arn" {
  description = "Managed-cell ECR writer role; empty only for the isolated Floci origin."
  type        = string
  default     = ""

  validation {
    condition     = var.workload_origin_role_arn == "" || can(regex("^arn:aws[a-z-]*:iam::[0-9]{12}:role/[A-Za-z0-9+=,.@_-]{1,64}$", var.workload_origin_role_arn))
    error_message = "workload_origin_role_arn must be empty or an exact AWS IAM role ARN."
  }
}

variable "workload_origin_token_audience" {
  description = "Projected token audience for external managed cells; empty for Floci and EKS."
  type        = string
  default     = ""

  validation {
    condition     = contains(["", "sts.amazonaws.com"], var.workload_origin_token_audience)
    error_message = "workload_origin_token_audience must be empty or sts.amazonaws.com."
  }
}

variable "workload_origin_token_file" {
  description = "Projected web-identity token path for external managed cells; empty for Floci and EKS."
  type        = string
  default     = ""

  validation {
    condition     = contains(["", "/var/run/secrets/workload-origin/token"], var.workload_origin_token_file)
    error_message = "workload_origin_token_file must be empty or the canonical workload-origin token path."
  }
}

variable "workspace_image" {
  description = "Multi-architecture minimal dev OCI image, pinned by sha256 digest."
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9._:/-]*@sha256:[0-9a-f]{64}$", var.workspace_image))
    error_message = "workspace_image must be an OCI repository plus sha256 digest."
  }
}

# LINT.IfChange(workspace-backup-proxy-template-image)
variable "workspace_backup_proxy_image" {
  description = "Multi-architecture workspace backup proxy image, pinned by sha256 digest."
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9._:/-]*@sha256:[0-9a-f]{64}$", var.workspace_backup_proxy_image))
    error_message = "workspace_backup_proxy_image must be an OCI repository plus sha256 digest."
  }
}
# LINT.ThenChange(//src/infra/definitions/workspaces/templates/dev/deployment.tf:workspace-backup-proxy-runtime-image)

variable "workspace_arch" {
  description = "Architecture selected for the workspace agent and the multi-architecture image."
  type        = string

  validation {
    condition     = contains(["amd64", "arm64"], var.workspace_arch)
    error_message = "workspace_arch must be amd64 or arm64."
  }
}

variable "workspace_incarnation_inventory" {
  description = "Canonical JSON map from each eligible cluster to its immutable cell incarnation."
  type        = string

  validation {
    condition = try(
      length(keys(jsondecode(var.workspace_incarnation_inventory))) > 0 &&
      var.workspace_incarnation_inventory == jsonencode(jsondecode(var.workspace_incarnation_inventory)) &&
      alltrue([
        for cluster, incarnation in jsondecode(var.workspace_incarnation_inventory) :
        can(regex("^[a-z][a-z0-9-]{1,61}[a-z0-9]$", cluster)) &&
        can(regex("^[0-9a-f]{12}$", incarnation))
      ]),
      false,
    )
    error_message = "workspace_incarnation_inventory must be a non-empty canonical JSON object mapping cluster names to 12-character lowercase hexadecimal incarnations."
  }
}

variable "workspace_placement_inventory" {
  description = "Canonical JSON map from each eligible cluster to its declared workspace resource envelope."
  type        = string

  validation {
    condition = try(
      length(keys(jsondecode(var.workspace_placement_inventory))) > 0 &&
      var.workspace_placement_inventory == jsonencode(jsondecode(var.workspace_placement_inventory)),
      false,
    )
    error_message = "workspace_placement_inventory must be a non-empty canonical JSON object."
  }
}

variable "workspace_virtual_name_inventory" {
  description = "Canonical JSON map from each eligible cluster to its storage-gateway virtual name."
  type        = string

  validation {
    condition = try(
      length(keys(jsondecode(var.workspace_virtual_name_inventory))) > 0 &&
      var.workspace_virtual_name_inventory == jsonencode(jsondecode(var.workspace_virtual_name_inventory)),
      false,
    )
    error_message = "workspace_virtual_name_inventory must be a non-empty canonical JSON object."
  }
}


variable "workspace_service_account" {
  description = "Existing ServiceAccount in the workspaces namespace used by workspace pods."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,61}[a-z0-9]$", var.workspace_service_account))
    error_message = "workspace_service_account must be a lowercase DNS label."
  }
}

variable "kubernetes_config_path" {
  description = "Provisioner-local kubeconfig path; the OSS built-in provisioner receives the scoped primary-cell credential here."
  type        = string
  default     = "/var/run/coder/cell/kubeconfig"

  validation {
    condition     = startswith(var.kubernetes_config_path, "/")
    error_message = "kubernetes_config_path must be absolute."
  }
}
