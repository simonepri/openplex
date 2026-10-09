# Tests IAM role configuration, permissions boundaries, and policy attachments in the AWS identity component.

mock_provider "aws" {
  mock_resource "aws_iam_role" {
    defaults = {
      arn = "arn:aws:iam::123456789012:role/mock"
    }
  }

  mock_data "aws_caller_identity" {
    defaults = {
      account_id = "123456789012"
      arn        = "arn:aws:iam::123456789012:root"
    }
  }

  mock_data "aws_region" {
    defaults = {
      name   = "us-west-2"
      region = "us-west-2"
    }
  }
}

variables {
  cluster_name             = "test-cluster"
  cluster_oidc_issuer_url  = "https://oidc.eks.us-west-2.amazonaws.com/id/MOCK"
  cluster_oidc_arn         = "arn:aws:iam::123456789012:oidc-provider/oidc.eks.us-west-2.amazonaws.com/id/MOCK"
  iam_name_prefix          = "corp-"
  iam_permissions_boundary = "arn:aws:iam::123456789012:policy/boundary"

  roles = {
    load-balancer = {
      namespace       = "kube-system"
      service_account = "aws-load-balancer-controller"
    }
    legacy-research-data = {
      namespace       = "s3-system"
      service_account = "s3-gateway-legacy-research-data"
    }
    external-dns = {
      namespace       = "external-dns-system"
      service_account = "external-dns"
    }
    storage-stats = {
      namespace       = "s3-system"
      service_account = "s3-gateway-storage-stats"
    }
    workspace-backups = {
      namespace       = "s3-system"
      service_account = "s3-gateway-org-workspace-backups"
    }
    db-backups = {
      namespace       = "database"
      service_account = "barman"
    }
    opencost = {
      namespace       = "opencost"
      service_account = "opencost"
    }
    trivy = {
      namespace       = "trivy-system"
      service_account = "trivy-operator"
    }
  }
}

run "verifies_iam_names_and_permissions_boundary" {
  command = plan

  assert {
    condition     = aws_iam_role.this["load-balancer"].name == "corp-test-cluster-load-balancer"
    error_message = "IAM role name must prepend iam_name_prefix and hyphenate keys."
  }

  assert {
    condition     = aws_iam_role.this["load-balancer"].permissions_boundary == "arn:aws:iam::123456789012:policy/boundary"
    error_message = "Permissions boundary must be attached to the IAM role."
  }

  assert {
    condition     = aws_iam_role.this["legacy-research-data"].name == "corp-test-cluster-legacy-research-data"
    error_message = "legacy-research-data role name must carry the IAM name prefix."
  }

  assert {
    condition     = aws_iam_role.this["external-dns"].name == "corp-test-cluster-external-dns"
    error_message = "external-dns role name must match hyphenated key."
  }

  assert {
    condition     = aws_iam_role.this["storage-stats"].name == "corp-test-cluster-storage-stats"
    error_message = "storage-stats role name must match hyphenated key."
  }

  assert {
    condition     = aws_iam_role.this["workspace-backups"].name == "corp-test-cluster-workspace-backups"
    error_message = "workspace-backups role name must match hyphenated key."
  }

  assert {
    condition     = aws_iam_role.this["db-backups"].name == "corp-test-cluster-db-backups"
    error_message = "db-backups role name must match hyphenated key."
  }

  assert {
    condition     = aws_iam_role.this["opencost"].name == "corp-test-cluster-opencost"
    error_message = "opencost role name must match hyphenated key."
  }

  assert {
    condition     = aws_iam_role.this["trivy"].name == "corp-test-cluster-trivy"
    error_message = "trivy role name must match hyphenated key."
  }

  assert {
    condition     = output.atlantis_plan_role_arn == ""
    error_message = "atlantis_plan_role_arn output must be empty when atlantis is not in roles."
  }

  assert {
    condition     = output.atlantis_apply_role_arn == ""
    error_message = "atlantis_apply_role_arn output must be empty when atlantis is not in roles."
  }
}

run "verifies_default_empty_prefix_and_null_boundary" {
  command = plan

  variables {
    iam_name_prefix          = ""
    iam_permissions_boundary = null
  }

  assert {
    condition     = aws_iam_role.this["load-balancer"].name == "test-cluster-load-balancer"
    error_message = "IAM role name with empty prefix must be cluster-purpose."
  }

  assert {
    condition     = aws_iam_role.this["load-balancer"].permissions_boundary == null
    error_message = "Permissions boundary must default to null."
  }

  assert {
    condition     = aws_iam_role.this["legacy-research-data"].name == "test-cluster-legacy-research-data"
    error_message = "legacy-research-data role name must be test-cluster-legacy-research-data."
  }
}

run "verifies_workspaces_policy_removed" {
  command = plan

  variables {
    roles = {
      workspaces = {
        namespace       = "s3-system"
        service_account = "s3-gateway-workspaces"
      }
    }
  }

  assert {
    condition     = !contains(keys(aws_iam_role_policy.scoped), "workspaces")
    error_message = "workspaces role must not have a scoped IAM role policy anymore."
  }
}

run "verifies_snapshot_portal_reads_cell_backups" {
  command = plan

  variables {
    roles = {
      snapshot-portal = {
        namespace       = "coder-workspace-backup-system"
        service_account = "coder-snapshot-portal"
      }
    }
  }

  assert {
    condition     = contains(jsondecode(aws_iam_role_policy.scoped["snapshot-portal"].policy).Statement[0].Resource, "arn:aws:s3:::cell-*-backups-123456789012")
    error_message = "snapshot-portal policy must include cell backups bucket pattern."
  }
}

run "verifies_kargo_reads_only_workload_images" {
  command = plan

  variables {
    roles = {
      kargo = {
        namespace       = "kargo"
        service_account = "kargo-controller"
      }
    }
  }

  assert {
    condition     = jsondecode(aws_iam_role_policy.scoped["kargo"].policy).Statement[1].Resource == "arn:aws:ecr:us-west-2:123456789012:repository/src/*"
    error_message = "kargo policy must scope image discovery to this account's workload repositories."
  }

  assert {
    condition     = length([for a in jsondecode(aws_iam_role_policy.scoped["kargo"].policy).Statement[1].Action : a if contains(["ecr:PutImage", "ecr:InitiateLayerUpload", "ecr:DeleteRepository", "ecr:BatchDeleteImage"], a)]) == 0
    error_message = "kargo policy must stay read-only."
  }
}

run "verifies_team_reader_policy_read_only" {
  command = plan

  variables {
    cluster_name            = "test-cluster"
    storage_kms_key_arn     = "arn:aws:kms:us-west-2:123456789012:key/mock"
    storage_meta_bucket_arn = "arn:aws:s3:::test-cluster-meta-123456789012"
    roles = {
      s3-gateway-examples-reader = {
        namespace       = "s3-system"
        service_account = "s3-gateway-examples-reader"
      }
    }
  }

  assert {
    condition     = aws_iam_role.this["s3-gateway-examples-reader"].name == "corp-test-cluster-s3-gateway-examples-reader"
    error_message = "IAM role name for team reader must match cluster and role key."
  }

  assert {
    condition     = length(jsondecode(aws_iam_role_policy.scoped["s3-gateway-examples-reader"].policy).Statement) == 7
    error_message = "Team reader policy must contain 7 statements."
  }

  assert {
    condition     = contains(jsondecode(aws_iam_role_policy.scoped["s3-gateway-examples-reader"].policy).Statement[1].Action, "s3:GetObject") && !contains(jsondecode(aws_iam_role_policy.scoped["s3-gateway-examples-reader"].policy).Statement[1].Action, "s3:PutObject")
    error_message = "Team reader home object access must allow GetObject and not PutObject."
  }

  assert {
    condition     = contains(jsondecode(aws_iam_role_policy.scoped["s3-gateway-examples-reader"].policy).Statement[1].Resource, "arn:aws:s3:::test-cluster-home-123456789012/home/examples/*")
    error_message = "Team reader home prefix must be home/examples/* and not home/examples-reader/*."
  }

  assert {
    condition     = contains(jsondecode(aws_iam_role_policy.scoped["s3-gateway-examples-reader"].policy).Statement[6].Action, "kms:Decrypt") && !contains(jsondecode(aws_iam_role_policy.scoped["s3-gateway-examples-reader"].policy).Statement[6].Action, "kms:Encrypt")
    error_message = "Team reader KMS access must allow Decrypt and not Encrypt."
  }
}

run "verifies_atlantis_pod_role_trust_and_actions" {
  command = plan

  variables {
    roles = {
      atlantis = {
        namespace       = "atlantis"
        service_account = "atlantis-apply"
      }
    }
  }

  assert {
    condition     = aws_iam_role.atlantis_plan[0].name == "corp-test-cluster-atlantis-plan"
    error_message = "Atlantis plan IAM role name must match contract."
  }

  assert {
    condition     = aws_iam_role.atlantis_apply[0].name == "corp-test-cluster-atlantis-apply"
    error_message = "Atlantis apply IAM role name must match contract."
  }

  assert {
    condition     = aws_iam_role.atlantis_plan[0].permissions_boundary == "arn:aws:iam::123456789012:policy/boundary"
    error_message = "Permissions boundary must be attached to the Atlantis plan role."
  }

  assert {
    condition     = aws_iam_role.atlantis_apply[0].permissions_boundary == "arn:aws:iam::123456789012:policy/boundary"
    error_message = "Permissions boundary must be attached to the Atlantis apply role."
  }

  assert {
    condition     = jsondecode(aws_iam_role.atlantis_plan[0].assume_role_policy).Statement[0].Principal.AWS == aws_iam_role.this["atlantis"].arn
    error_message = "Atlantis plan role must trust only the pod identity role."
  }

  assert {
    condition     = jsondecode(aws_iam_role.atlantis_apply[0].assume_role_policy).Statement[0].Principal.AWS == aws_iam_role.this["atlantis"].arn
    error_message = "Atlantis apply role must trust only the pod identity role."
  }

  assert {
    condition = toset(flatten([
      for s in jsondecode(aws_iam_role_policy.scoped["atlantis"].policy).Statement : s.Action
    ])) == toset(["sts:AssumeRole", "sts:TagSession"])
    error_message = "Atlantis pod role policy must only grant sts:AssumeRole and sts:TagSession."
  }

  assert {
    condition = toset(flatten([
      for s in jsondecode(aws_iam_role_policy.scoped["atlantis"].policy).Statement : s.Resource
    ])) == toset([
      aws_iam_role.atlantis_plan[0].arn,
      aws_iam_role.atlantis_apply[0].arn,
    ])
    error_message = "Atlantis pod role policy must only target atlantis-plan and atlantis-apply role ARNs."
  }

  assert {
    condition     = output.atlantis_plan_role_arn == aws_iam_role.atlantis_plan[0].arn
    error_message = "atlantis_plan_role_arn output must match plan role ARN when atlantis is configured."
  }

  assert {
    condition     = output.atlantis_apply_role_arn == aws_iam_role.atlantis_apply[0].arn
    error_message = "atlantis_apply_role_arn output must match apply role ARN when atlantis is configured."
  }
}

run "verifies_atlantis_plan_policy_read_only" {
  command = plan

  variables {
    roles = {
      atlantis = {
        namespace       = "atlantis"
        service_account = "atlantis-apply"
      }
    }
    opentofu_state_bucket = ""
  }

  assert {
    condition = length([
      for a in flatten([
        for s in jsondecode(aws_iam_role_policy.atlantis_plan[0].policy).Statement : s.Action
      ]) : a
      if can(regex("^([a-z0-9_-]+:(Create|Delete|Put|Update|Attach|Detach|Tag|Untag|RunInstances|TerminateInstances|PassRole))", a))
    ]) == 0
    error_message = "Atlantis plan policy must not contain write actions or PassRole."
  }

  assert {
    condition = length([
      for s in jsondecode(aws_iam_role_policy.atlantis_plan[0].policy).Statement : s
      if s.Sid == "AtlantisFleetSecretRead"
    ]) == 1
    error_message = "Atlantis plan policy must contain AtlantisFleetSecretRead statement."
  }

  assert {
    condition = contains(
      one([
        for s in jsondecode(aws_iam_role_policy.atlantis_plan[0].policy).Statement : s
        if s.Sid == "AtlantisFleetSecretRead"
      ]).Resource,
      "arn:aws:secretsmanager:*:*:secret:test-cluster-*"
    )
    error_message = "AtlantisFleetSecretRead statement must include cluster secrets pattern."
  }

  assert {
    condition = contains(
      one([
        for s in jsondecode(aws_iam_role_policy.atlantis_plan[0].policy).Statement : s
        if s.Sid == "AtlantisFleetSecretRead"
      ]).Resource,
      "arn:aws:secretsmanager:*:*:secret:cell-*"
    )
    error_message = "AtlantisFleetSecretRead statement must include cell secrets pattern."
  }

  assert {
    condition = length([
      for s in jsondecode(aws_iam_role_policy.atlantis_plan[0].policy).Statement : s
      if s.Sid == "AtlantisFleetSecretDecrypt"
    ]) == 1
    error_message = "Atlantis plan policy must contain AtlantisFleetSecretDecrypt statement."
  }

  assert {
    condition = one([
      for s in jsondecode(aws_iam_role_policy.atlantis_plan[0].policy).Statement : s
      if s.Sid == "AtlantisFleetSecretDecrypt"
    ]).Resource == "*"
    error_message = "AtlantisFleetSecretDecrypt statement must target Resource *."
  }

  assert {
    condition = jsondecode(aws_iam_role_policy.atlantis_plan[0].policy).Statement[
      index([for s in jsondecode(aws_iam_role_policy.atlantis_plan[0].policy).Statement : s.Sid], "AtlantisFleetSecretDecrypt")
    ].Condition.StringLike["kms:ViaService"] == "secretsmanager.*.amazonaws.com"
    error_message = "AtlantisFleetSecretDecrypt statement must require ViaService secretsmanager.*.amazonaws.com."
  }
}

run "verifies_atlantis_plan_metadata_read" {
  command = plan

  variables {
    roles = {
      atlantis = {
        namespace       = "atlantis"
        service_account = "atlantis-apply"
      }
    }
  }

  assert {
    condition = length([
      for s in jsondecode(aws_iam_role_policy.atlantis_plan[0].policy).Statement : s
      if s.Sid == "AtlantisPlanMetadataRead"
    ]) == 1
    error_message = "Atlantis plan policy must contain an AtlantisPlanMetadataRead statement."
  }

  assert {
    condition = one([
      for s in jsondecode(aws_iam_role_policy.atlantis_plan[0].policy).Statement : s
      if s.Sid == "AtlantisPlanMetadataRead"
    ]).Resource == "*"
    error_message = "AtlantisPlanMetadataRead statement must have Resource *."
  }

  assert {
    condition = length([
      for a in one([
        for s in jsondecode(aws_iam_role_policy.atlantis_plan[0].policy).Statement : s
        if s.Sid == "AtlantisPlanMetadataRead"
      ]).Action : a
      if contains([
        "s3:GetObject",
        "secretsmanager:GetSecretValue",
        "logs:GetLogEvents",
        "athena:GetQueryResults",
        "ecr:BatchGetImage",
        "ec2:GetPasswordData",
        "*",
        "s3:Get*"
      ], a)
    ]) == 0
    error_message = "AtlantisPlanMetadataRead statement must not contain data-read actions, bare *, or s3:Get*."
  }
}

run "verifies_atlantis_apply_policy_includes_write_statements" {
  command = plan

  variables {
    roles = {
      atlantis = {
        namespace       = "atlantis"
        service_account = "atlantis-apply"
      }
    }
  }

  assert {
    condition = toset([
      for s in jsondecode(aws_iam_role_policy.atlantis_apply[0].policy).Statement : s.Sid
      if contains([
        "AtlantisEC2Management",
        "AtlantisEKSManagement",
        "AtlantisS3BucketManagement",
        "AtlantisRoute53Management",
        "AtlantisSecretsManagerManagement",
        "AtlantisKMSManagement",
        "AtlantisIAMRoleManagement",
        "AtlantisIAMOIDCManagement",
        "AtlantisFleetSecretKMSWrite",
      ], s.Sid)
    ]) == toset([
      "AtlantisEC2Management",
      "AtlantisEKSManagement",
      "AtlantisS3BucketManagement",
      "AtlantisRoute53Management",
      "AtlantisSecretsManagerManagement",
      "AtlantisKMSManagement",
      "AtlantisIAMRoleManagement",
      "AtlantisIAMOIDCManagement",
      "AtlantisFleetSecretKMSWrite",
    ])
    error_message = "Atlantis apply policy must include all write and management statements."
  }

  assert {
    condition = !contains(
      one([
        for s in jsondecode(aws_iam_role_policy.atlantis_apply[0].policy).Statement : s
        if s.Sid == "AtlantisSecretsManagerManagement"
      ]).Resource,
      "arn:aws:secretsmanager:*:*:secret:*"
    )
    error_message = "Atlantis secretsmanager statement must not contain bare secret:* resource."
  }

  assert {
    condition = contains(
      one([
        for s in jsondecode(aws_iam_role_policy.atlantis_apply[0].policy).Statement : s
        if s.Sid == "AtlantisSecretsManagerManagement"
      ]).Resource,
      "arn:aws:secretsmanager:*:*:secret:cell-*"
    )
    error_message = "Atlantis secretsmanager statement must include scoped cell secrets resource pattern."
  }

  assert {
    condition = !contains(
      flatten([
        for s in jsondecode(aws_iam_role_policy.atlantis_apply[0].policy).Statement : s.Resource
        if s.Sid == "AtlantisS3BucketManagement"
      ]),
      "arn:aws:s3:::*-tf-state*"
    )
    error_message = "Atlantis S3 bucket management must not reference *-tf-state*."
  }

  assert {
    condition = toset(
      one([
        for s in jsondecode(aws_iam_role_policy.atlantis_apply[0].policy).Statement : s
        if s.Sid == "AtlantisFleetSecretKMSWrite"
      ]).Action
    ) == toset(["kms:Decrypt", "kms:Encrypt", "kms:GenerateDataKey"])
    error_message = "AtlantisFleetSecretKMSWrite statement must grant Decrypt, Encrypt, and GenerateDataKey."
  }
}

run "verifies_atlantis_opentofu_state_policy" {
  command = plan

  variables {
    roles = {
      atlantis = {
        namespace       = "atlantis"
        service_account = "atlantis-apply"
      }
    }
    opentofu_state_bucket = "openplex-platform-opentofu-state-400920695547"
  }

  assert {
    condition = contains(
      one([
        for s in jsondecode(aws_iam_role_policy.atlantis_plan[0].policy).Statement : s
        if s.Sid == "AtlantisOpenTofuState"
      ]).Resource,
      "arn:aws:s3:::openplex-platform-opentofu-state-400920695547"
    )
    error_message = "Atlantis OpenTofu state statement in plan policy must include the bucket ARN."
  }

  assert {
    condition = contains(
      one([
        for s in jsondecode(aws_iam_role_policy.atlantis_plan[0].policy).Statement : s
        if s.Sid == "AtlantisOpenTofuState"
      ]).Resource,
      "arn:aws:s3:::openplex-platform-opentofu-state-400920695547/*"
    )
    error_message = "Atlantis OpenTofu state statement in plan policy must include the bucket object wildcard ARN."
  }

  assert {
    condition = toset(
      one([
        for s in jsondecode(aws_iam_role_policy.atlantis_plan[0].policy).Statement : s
        if s.Sid == "AtlantisOpenTofuState"
      ]).Action
    ) == toset(["s3:DeleteObject", "s3:GetObject", "s3:ListBucket", "s3:PutObject"])
    error_message = "Atlantis OpenTofu state statement in plan policy must grant only backend required S3 actions."
  }

  assert {
    condition = contains(
      one([
        for s in jsondecode(aws_iam_role_policy.atlantis_apply[0].policy).Statement : s
        if s.Sid == "AtlantisOpenTofuState"
      ]).Resource,
      "arn:aws:s3:::openplex-platform-opentofu-state-400920695547"
    )
    error_message = "Atlantis OpenTofu state statement in apply policy must include the bucket ARN."
  }

  assert {
    condition = contains(
      one([
        for s in jsondecode(aws_iam_role_policy.atlantis_apply[0].policy).Statement : s
        if s.Sid == "AtlantisOpenTofuState"
      ]).Resource,
      "arn:aws:s3:::openplex-platform-opentofu-state-400920695547/*"
    )
    error_message = "Atlantis OpenTofu state statement in apply policy must include the bucket object wildcard ARN."
  }

  assert {
    condition = toset(
      one([
        for s in jsondecode(aws_iam_role_policy.atlantis_apply[0].policy).Statement : s
        if s.Sid == "AtlantisOpenTofuState"
      ]).Action
    ) == toset(["s3:DeleteObject", "s3:GetObject", "s3:ListBucket", "s3:PutObject"])
    error_message = "Atlantis OpenTofu state statement in apply policy must grant only backend required S3 actions."
  }
}

run "verifies_atlantis_opentofu_state_policy_absent_when_empty" {
  command = plan

  variables {
    roles = {
      atlantis = {
        namespace       = "atlantis"
        service_account = "atlantis-apply"
      }
    }
    opentofu_state_bucket = ""
  }

  assert {
    condition = length([
      for s in jsondecode(aws_iam_role_policy.atlantis_plan[0].policy).Statement : s
      if s.Sid == "AtlantisOpenTofuState"
    ]) == 0
    error_message = "Atlantis OpenTofu state statement must be absent in plan policy when opentofu_state_bucket is empty."
  }

  assert {
    condition = length([
      for s in jsondecode(aws_iam_role_policy.atlantis_apply[0].policy).Statement : s
      if s.Sid == "AtlantisOpenTofuState"
    ]) == 0
    error_message = "Atlantis OpenTofu state statement must be absent in apply policy when opentofu_state_bucket is empty."
  }
}
