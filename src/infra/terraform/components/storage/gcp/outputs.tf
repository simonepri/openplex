# Exports GCS bucket names, storage URLs, and KMS crypto key identifiers.

output "record" {
  description = "Canonical storage record describing realized storage buckets."
  value       = module.interface.record
}

output "managed_folders" {
  description = "Managed folders provisioned for teams in home and scratch storage tiers."
  value = {
    home = {
      for team, folder in google_storage_managed_folder.home : team => {
        id     = folder.id
        name   = folder.name
        bucket = folder.bucket
      }
    }
    scratch = {
      for team, folder in google_storage_managed_folder.scratch : team => {
        id     = folder.id
        name   = folder.name
        bucket = folder.bucket
      }
    }
  }
}

output "managed_folder_iam_bindings" {
  description = "Folder-scoped IAM bindings provisioned for teams on managed folders."
  value = {
    home = {
      for team, binding in google_storage_managed_folder_iam_binding.home : team => {
        id             = binding.id
        bucket         = binding.bucket
        managed_folder = binding.managed_folder
        role           = binding.role
        members        = binding.members
      }
    }
    scratch = {
      for team, binding in google_storage_managed_folder_iam_binding.scratch : team => {
        id             = binding.id
        bucket         = binding.bucket
        managed_folder = binding.managed_folder
        role           = binding.role
        members        = binding.members
      }
    }
  }
}
