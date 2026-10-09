# Tests Cloudflare R2 bucket naming, token scoping, and S3 credential derivation.

mock_provider "cloudflare" {
  mock_resource "cloudflare_r2_bucket" {
    defaults = {
      id = "mock-bucket"
    }
  }

  mock_resource "cloudflare_r2_bucket_lifecycle" {
    defaults = {}
  }

  mock_resource "cloudflare_account_token" {
    defaults = {
      id    = "mock-token-id-123456789012345678"
      value = "mock-token-secret-value-abcdef123"
    }
  }
}

variables {
  account_id     = "test-account-id"
  name_prefix    = "ctrl-aws-usw2"
  account_suffix = "400920695547"
  location       = "wnam"
  teams          = ["alpha", "beta"]
}

run "verifies_bucket_names_and_location" {
  command = plan

  assert {
    condition     = cloudflare_r2_bucket.this["alpha"].name == "ctrl-aws-usw2-global-alpha-400920695547"
    error_message = "Bucket name for team alpha must match expected pattern."
  }

  assert {
    condition     = cloudflare_r2_bucket.this["beta"].name == "ctrl-aws-usw2-global-beta-400920695547"
    error_message = "Bucket name for team beta must match expected pattern."
  }

  assert {
    condition     = cloudflare_r2_bucket.this["alpha"].location == "wnam"
    error_message = "Location hint for team alpha must be wnam."
  }

  assert {
    condition     = cloudflare_r2_bucket.this["beta"].location == "wnam"
    error_message = "Location hint for team beta must be wnam."
  }
}

run "verifies_token_scoping_to_exactly_one_bucket" {
  command = plan

  assert {
    condition     = length(cloudflare_account_token.this["alpha"].policies) == 1
    error_message = "Token must have exactly one policy."
  }

  assert {
    condition     = length(keys(jsondecode(cloudflare_account_token.this["alpha"].policies[0].resources))) == 1
    error_message = "Token resources map must contain exactly one bucket resource key."
  }

  assert {
    condition     = contains(keys(jsondecode(cloudflare_account_token.this["alpha"].policies[0].resources)), "com.cloudflare.edge.r2.bucket.test-account-id_default_ctrl-aws-usw2-global-alpha-400920695547")
    error_message = "Token for team alpha must be scoped specifically to ctrl-aws-usw2-global-alpha-400920695547."
  }

  assert {
    condition     = contains(keys(jsondecode(cloudflare_account_token.this["beta"].policies[0].resources)), "com.cloudflare.edge.r2.bucket.test-account-id_default_ctrl-aws-usw2-global-beta-400920695547")
    error_message = "Token for team beta must be scoped specifically to ctrl-aws-usw2-global-beta-400920695547."
  }
}

run "verifies_sha256_secret_derivation_and_outputs" {
  command = plan

  assert {
    condition     = output.endpoint == "https://test-account-id.r2.cloudflarestorage.com"
    error_message = "Output endpoint must match Cloudflare R2 account endpoint."
  }

  assert {
    condition     = output.teams["alpha"].bucket == "ctrl-aws-usw2-global-alpha-400920695547"
    error_message = "Output team alpha bucket must match bucket name."
  }

  assert {
    condition     = output.teams["alpha"].access_key_id == "mock-token-id-123456789012345678"
    error_message = "Output team alpha access_key_id must match token id."
  }

  assert {
    condition     = output.teams["alpha"].secret_access_key == sha256("mock-token-secret-value-abcdef123")
    error_message = "Output team alpha secret_access_key must be sha256(token.value)."
  }

  assert {
    condition     = output.teams["alpha"].endpoint == "https://test-account-id.r2.cloudflarestorage.com"
    error_message = "Output team alpha endpoint must match Cloudflare R2 account endpoint."
  }
}

run "verifies_lifecycle_rules" {
  command = plan

  assert {
    condition     = length(cloudflare_r2_bucket_lifecycle.this["alpha"].rules) == 2
    error_message = "Lifecycle configuration must have exactly two rules."
  }

  assert {
    condition     = cloudflare_r2_bucket_lifecycle.this["alpha"].rules[0].conditions.prefix == "" && cloudflare_r2_bucket_lifecycle.this["alpha"].rules[0].abort_multipart_uploads_transition.condition.max_age == 604800
    error_message = "First lifecycle rule must abort multipart uploads after 7 days (604800 seconds)."
  }

  assert {
    condition     = cloudflare_r2_bucket_lifecycle.this["alpha"].rules[1].conditions.prefix == "scratch/" && cloudflare_r2_bucket_lifecycle.this["alpha"].rules[1].delete_objects_transition.condition.max_age == 2592000
    error_message = "Second lifecycle rule must expire scratch/ after 30 days (2592000 seconds)."
  }
}

run "verifies_reader_token_scoping_read_only" {
  command = plan

  assert {
    condition     = length(cloudflare_account_token.reader["alpha"].policies) == 1
    error_message = "Reader token must have exactly one policy."
  }

  assert {
    condition     = length(cloudflare_account_token.reader["alpha"].policies[0].permission_groups) == 1
    error_message = "Reader token must have exactly one permission group."
  }

  assert {
    condition     = cloudflare_account_token.reader["alpha"].policies[0].permission_groups[0].id == "6a018a9f2fc74eb6b293b0c548f38b39"
    error_message = "Reader token permission group must be read-only (6a018a9f2fc74eb6b293b0c548f38b39)."
  }

  assert {
    condition     = contains(keys(jsondecode(cloudflare_account_token.reader["alpha"].policies[0].resources)), "com.cloudflare.edge.r2.bucket.test-account-id_default_ctrl-aws-usw2-global-alpha-400920695547")
    error_message = "Reader token for team alpha must be scoped specifically to ctrl-aws-usw2-global-alpha-400920695547."
  }

  assert {
    condition     = output.readers["alpha"].bucket == "ctrl-aws-usw2-global-alpha-400920695547"
    error_message = "Output readers alpha bucket must match bucket name."
  }

  assert {
    condition     = output.readers["alpha"].access_key_id == "mock-token-id-123456789012345678"
    error_message = "Output readers alpha access_key_id must match token id."
  }

  assert {
    condition     = output.readers["alpha"].secret_access_key == sha256("mock-token-secret-value-abcdef123")
    error_message = "Output readers alpha secret_access_key must be sha256(token.value)."
  }
}
