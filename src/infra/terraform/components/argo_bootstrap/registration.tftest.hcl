# Tests that computed registered-cells takes precedence over caller-provided annotations.

variables {
  cluster_name           = "test-ctrl"
  cluster_endpoint       = "https://kubernetes.default.svc"
  cluster_ca_certificate = "LS0tLS1CRUdJTiBDRVJUSUZJQ0FURS0tLS0tCk1JSUMzakNDQWNhZ0F3SUJBZ0lVRnlZZFNqZ29mNmtuQ2gvTFV5N21GNkVQKy84d0RRWUpLb1pJaHZjTkFRRUwKQlFBd0R6RU5NQXNHQTFVRUF3d0VkR1Z6ZERBZUZ3MHlOakV3TURNeE1ETTVNak5hRncweU5qRXdNRFF4TURNNQpNak5hTUE4eERUQUxCZ05WQkFNTUJIUmxjM1F3Z2dFaU1BMEdDU3FHU0liM0RRRUJBUVVBQTRJQkR3QXdnZ0VLCkFvSUJBUUNWS284QXJEb201WVBjdVNXY05FUXl4blo2amY1WUVNRE5rTWtSVUZsNUFyYnp0VitvOEVsZWZFR1MKaG5zUGxaOHBKY2ZhMUo0UGROeVhlK2h6ZnFzNHZPdDBhTkhBNkthTTdmejloZlpoM21LWktPK0pybjM2SkZKRgpMSE5TQUo4L1hZL2xzY2tyRWpjNlA0ZVhldVIyOXJZbktTOEZEbWdnUjFiR3BkdUxrU1VPQXFWdWY0TXptWUUyClRqbzVtRy9mY0kvVkR0WWI3OEN0ekdTRk5sU1hBc1pTRmY1N2c3RW5lbnVuTGVxNW5RZWdha091ZE9GZmtMV1AKK291YzBLYi80VjdLMWF1QzhKQzg5T1JzQVM2L3NlUnpvSHEwcDBPZmdoY2QyL3cvc2N1L0krbEVzSjhDRXJCeApQU2pjNy96RE9kMmxjbGtSbm44TUx6UkcvZ3VCQWdNQkFBR2pNakF3TUIwR0ExVWREZ1FXQkJRdFFiVzhlTmhTClZPUUViMXdDcDF6aWlidmNmakFQQmdOVkhSTUJBZjhFQlRBREFRSC9NQTBHQ1NxR1NJYjNEUUVCQ3dVQUE0SUIKQVFCVFNvYzBnc2ZhL1hCcmpQdlVwSFMrRzB4bkc2Y0JYTTZzZWlzSTM5bGpMVG1RYzJ4dzMwVlhhcXVGMk1HRwpSUktLM0w3d2E1ZVJRaTNlYXo1VWtlZmYyN1MzUU9ocWF2aFYwbXhtN1BpYnY5bE1DQ3VBaHZzd3YxZS94MG1zCkdGckhkNGVsZ293QWZwVmNQd0o0cFFCZGRvVUVHZ0NvZ3dyQ1JMUkxoL2w5RmthQSs3NVh6UTFRcW1LeUhRcHEKS3JHaFNjWjJVcHg2TG9nMGw1dUxtMUtWMC8zUytzRjBLV0k0KzFZelJTRFFDZ1pFVUtrQURNejdhRW1tSzFOTgp5N3RhMDNoTEg0SC9WUnMrWFI4NW1DbWRncER3b0NXRS9oSW0yM05FNG53UVRwaDhpYW1qZ2JscDh4UW81WGVqCmowZ1dWa3Zmc0lhVER3T1VLcDZ1R2wwVwotLS0tLUVORCBDRVJUSUZJQ0FURS0tLS0tCg=="
  public_domain_name     = "example.com"
  git_repo_url           = "https://github.com/example/fleet.git"
}

provider "helm" {
  kubernetes = {
    host                   = var.cluster_endpoint
    cluster_ca_certificate = base64decode(var.cluster_ca_certificate)
    insecure               = true
  }
}

provider "kubernetes" {
  host                   = var.cluster_endpoint
  cluster_ca_certificate = base64decode(var.cluster_ca_certificate)
  insecure               = true
}

run "registered_cells_wins_over_caller_annotations" {
  command = plan

  variables {
    registered_cells = [
      {
        name           = "cell-b"
        endpoint       = "https://cell-b.default.svc"
        ca_certificate = "LS0tLS1CRUdJTiBDRVJUSUZJQ0FURS0tLS0tCk1JSUMzakNDQWNhZ0F3SUJBZ0lVRnlZZFNqZ29mNmtuQ2gvTFV5N21GNkVQKy84d0RRWUpLb1pJaHZjTkFRRUwKQlFBd0R6RU5NQXNHQTFVRUF3d0VkR1Z6ZERBZUZ3MHlOakV3TURNeE1ETTVNak5hRncweU5qRXdNRFF4TURNNQpNak5hTUE4eERUQUxCZ05WQkFNTUJIUmxjM1F3Z2dFaU1BMEdDU3FHU0liM0RRRUJBUVVBQTRJQkR3QXdnZ0VLCkFvSUJBUUNWS284QXJEb201WVBjdVNXY05FUXl4blo2amY1WUVNRE5rTWtSVUZsNUFyYnp0VitvOEVsZWZFR1MKaG5zUGxaOHBKY2ZhMUo0UGROeVhlK2h6ZnFzNHZPdDBhTkhBNkthTTdmejloZlpoM21LWktPK0pybjM2SkZKRgpMSE5TQUo4L1hZL2xzY2tyRWpjNlA0ZVhldVIyOXJZbktTOEZEbWdnUjFiR3BkdUxrU1VPQXFWdWY0TXptWUUyClRqbzVtRy9mY0kvVkR0WWI3OEN0ekdTRk5sU1hBc1pTRmY1N2c3RW5lbnVuTGVxNW5RZWdha091ZE9GZmtMV1AKK291YzBLYi80VjdLMWF1QzhKQzg5T1JzQVM2L3NlUnpvSHEwcDBPZmdoY2QyL3cvc2N1L0krbEVzSjhDRXJCeApQU2pjNy96RE9kMmxjbGtSbm44TUx6UkcvZ3VCQWdNQkFBR2pNakF3TUIwR0ExVWREZ1FXQkJRdFFiVzhlTmhTClZPUUViMXdDcDF6aWlidmNmakFQQmdOVkhSTUJBZjhFQlRBREFRSC9NQTBHQ1NxR1NJYjNEUUVCQ3dVQUE0SUIKQVFCVFNvYzBnc2ZhL1hCcmpQdlVwSFMrRzB4bkc2Y0JYTTZzZWlzSTM5bGpMVG1RYzJ4dzMwVlhhcXVGMk1HRwpSUktLM0w3d2E1ZVJRaTNlYXo1VWtlZmYyN1MzUU9ocWF2aFYwbXhtN1BpYnY5bE1DQ3VBaHZzd3YxZS94MG1zCkdGckhkNGVsZ293QWZwVmNQd0o0cFFCZGRvVUVHZ0NvZ3dyQ1JMUkxoL2w5RmthQSs3NVh6UTFRcW1LeUhRcHEKS3JHaFNjWjJVcHg2TG9nMGw1dUxtMUtWMC8zUytzRjBLV0k0KzFZelJTRFFDZ1pFVUtrQURNejdhRW1tSzFOTgp5N3RhMDNoTEg0SC9WUnMrWFI4NW1DbWRncER3b0NXRS9oSW0yM05FNG53UVRwaDhpYW1qZ2JscDh4UW81WGVqCmowZ1dWa3Zmc0lhVER3T1VLcDZ1R2wwVwotLS0tLUVORCBDRVJUSUZJQ0FURS0tLS0tCg=="
      },
      {
        name           = "cell-a"
        endpoint       = "https://cell-a.default.svc"
        ca_certificate = "LS0tLS1CRUdJTiBDRVJUSUZJQ0FURS0tLS0tCk1JSUMzakNDQWNhZ0F3SUJBZ0lVRnlZZFNqZ29mNmtuQ2gvTFV5N21GNkVQKy84d0RRWUpLb1pJaHZjTkFRRUwKQlFBd0R6RU5NQXNHQTFVRUF3d0VkR1Z6ZERBZUZ3MHlOakV3TURNeE1ETTVNak5hRncweU5qRXdNRFF4TURNNQpNak5hTUE4eERUQUxCZ05WQkFNTUJIUmxjM1F3Z2dFaU1BMEdDU3FHU0liM0RRRUJBUVVBQTRJQkR3QXdnZ0VLCkFvSUJBUUNWS284QXJEb201WVBjdVNXY05FUXl4blo2amY1WUVNRE5rTWtSVUZsNUFyYnp0VitvOEVsZWZFR1MKaG5zUGxaOHBKY2ZhMUo0UGROeVhlK2h6ZnFzNHZPdDBhTkhBNkthTTdmejloZlpoM21LWktPK0pybjM2SkZKRgpMSE5TQUo4L1hZL2xzY2tyRWpjNlA0ZVhldVIyOXJZbktTOEZEbWdnUjFiR3BkdUxrU1VPQXFWdWY0TXptWUUyClRqbzVtRy9mY0kvVkR0WWI3OEN0ekdTRk5sU1hBc1pTRmY1N2c3RW5lbnVuTGVxNW5RZWdha091ZE9GZmtMV1AKK291YzBLYi80VjdLMWF1QzhKQzg5T1JzQVM2L3NlUnpvSHEwcDBPZmdoY2QyL3cvc2N1L0krbEVzSjhDRXJCeApQU2pjNy96RE9kMmxjbGtSbm44TUx6UkcvZ3VCQWdNQkFBR2pNakF3TUIwR0ExVWREZ1FXQkJRdFFiVzhlTmhTClZPUUViMXdDcDF6aWlidmNmakFQQmdOVkhSTUJBZjhFQlRBREFRSC9NQTBHQ1NxR1NJYjNEUUVCQ3dVQUE0SUIKQVFCVFNvYzBnc2ZhL1hCcmpQdlVwSFMrRzB4bkc2Y0JYTTZzZWlzSTM5bGpMVG1RYzJ4dzMwVlhhcXVGMk1HRwpSUktLM0w3d2E1ZVJRaTNlYXo1VWtlZmYyN1MzUU9ocWF2aFYwbXhtN1BpYnY5bE1DQ3VBaHZzd3YxZS94MG1zCkdGckhkNGVsZ293QWZwVmNQd0o0cFFCZGRvVUVHZ0NvZ3dyQ1JMUkxoL2w5RmthQSs3NVh6UTFRcW1LeUhRcHEKS3JHaFNjWjJVcHg2TG9nMGw1dUxtMUtWMC8zUytzRjBLV0k0KzFZelJTRFFDZ1pFVUtrQURNejdhRW1tSzFOTgp5N3RhMDNoTEg0SC9WUnMrWFI4NW1DbWRncER3b0NXRS9oSW0yM05FNG53UVRwaDhpYW1qZ2JscDh4UW81WGVqCmowZ1dWa3Zmc0lhVER3T1VLcDZ1R2wwVwotLS0tLUVORCBDRVJUSUZJQ0FURS0tLS0tCg=="
        annotations = {
          "registered-cells" = "stale-cell-y"
        }
      }
    ]
    annotations = {
      "registered-cells" = "stale-cell-x"
      "custom-key"       = "custom-value"
    }
  }

  assert {
    condition     = kubernetes_secret_v1.control_registration.metadata[0].annotations["registered-cells"] == "cell-a,cell-b"
    error_message = "Computed registered-cells annotation must win over caller annotations in control_registration"
  }

  assert {
    condition     = kubernetes_secret_v1.control_registration.metadata[0].annotations["custom-key"] == "custom-value"
    error_message = "Caller annotations other than registered-cells must be preserved"
  }

  assert {
    condition     = kubernetes_secret_v1.cell_registration["cell-a"].metadata[0].annotations["registered-cells"] == "cell-a,cell-b"
    error_message = "Computed registered-cells annotation must match sorted cell list in cell_registration"
  }
}

run "empty_registered_cells_wins_over_caller_annotations" {
  command = plan

  variables {
    registered_cells = []
    annotations = {
      "registered-cells" = "cell-hardcoded-should-be-cleared"
    }
  }

  assert {
    condition     = kubernetes_secret_v1.control_registration.metadata[0].annotations["registered-cells"] == ""
    error_message = "Empty registered_cells must produce empty registered-cells annotation even if caller supplied one"
  }
}

run "verifies_atlantis_role_annotations" {
  command = plan

  variables {
    atlantis_plan_role_arn  = "arn:aws:iam::123456789012:role/ctrl-aws-usw2-atlantis-plan"
    atlantis_apply_role_arn = "arn:aws:iam::123456789012:role/ctrl-aws-usw2-atlantis-apply"
  }

  assert {
    condition     = kubernetes_secret_v1.control_registration.metadata[0].annotations["atlantis-plan-role-arn"] == "arn:aws:iam::123456789012:role/ctrl-aws-usw2-atlantis-plan"
    error_message = "atlantis-plan-role-arn annotation must match provided atlantis_plan_role_arn"
  }

  assert {
    condition     = kubernetes_secret_v1.control_registration.metadata[0].annotations["atlantis-apply-role-arn"] == "arn:aws:iam::123456789012:role/ctrl-aws-usw2-atlantis-apply"
    error_message = "atlantis-apply-role-arn annotation must match provided atlantis_apply_role_arn"
  }
}
