// Test OpenTofu AST parsing, provider detection, module extraction, and variable resolution.

package opentofu

import (
	"os"
	"path/filepath"
	"reflect"
	"testing"
)

func TestParseProviders(t *testing.T) {
	dir := t.TempDir()
	versionsTf := filepath.Join(dir, "versions.tf")

	content := `
terraform {
  required_version = ">= 1.8.0"
  required_providers {
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "~> 3.0"
    }
    helm = {
      source  = "hashicorp/helm"
      version = "~> 3.0"
    }
  }
}
`
	if err := os.WriteFile(versionsTf, []byte(content), 0644); err != nil {
		t.Fatalf("failed to write test file: %v", err)
	}

	providers, err := parseProviders(versionsTf)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	expected := []string{"helm", "kubernetes"}
	if !reflect.DeepEqual(providers, expected) {
		t.Errorf("expected %v, got %v", expected, providers)
	}
}

func TestParseModuleDependencies(t *testing.T) {
	repoRoot := t.TempDir()
	componentDir := filepath.Join(repoRoot, "src", "infra", "terraform", "components", "cluster", "aws")
	targetDir := filepath.Join(repoRoot, "src", "infra", "terraform", "components", "cluster", "_interface")

	if err := os.MkdirAll(componentDir, 0755); err != nil {
		t.Fatalf("failed to create dir: %v", err)
	}
	if err := os.MkdirAll(targetDir, 0755); err != nil {
		t.Fatalf("failed to create dir: %v", err)
	}

	mainTf := filepath.Join(componentDir, "main.tf")
	content := `
module "_interface" {
  source = "../_interface"
}
`
	if err := os.WriteFile(mainTf, []byte(content), 0644); err != nil {
		t.Fatalf("failed to write test file: %v", err)
	}

	deps, err := parseModuleDependencies(componentDir, repoRoot, []string{"main.tf"})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	expected := []string{"//src/infra/terraform/components/cluster/_interface"}
	if !reflect.DeepEqual(deps, expected) {
		t.Errorf("expected %v, got %v", expected, deps)
	}
}
