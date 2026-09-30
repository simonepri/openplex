// Test parsing and dependency extraction for Kustomization files and vendor Helm chart definitions.

package gitops

import (
	"os"
	"path/filepath"
	"testing"
)

func TestParseVendorCharts(t *testing.T) {
	dir := t.TempDir()
	moduleFile := filepath.Join(dir, "vendor_helm_charts.MODULE.bazel")
	content := `
vendor_helm_chart(
    name = "vendor_helm_atlantis",
    chart_name = "atlantis",
    repository = "https://runatlantis.github.io/helm-charts",
    version = "6.15.0",
)

vendor_helm_file(
    name = "vendor_helm_cert_manager",
    downloaded_file_path = "cert-manager-v1.21.1.tgz",
    urls = ["https://charts.jetstack.io/charts/cert-manager-v1.21.1.tgz"],
)
`
	if err := os.WriteFile(moduleFile, []byte(content), 0644); err != nil {
		t.Fatalf("failed to write test file: %v", err)
	}

	charts, err := parseVendorCharts(moduleFile)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	atlantis, ok := charts["atlantis"]
	if !ok || atlantis.ChartName != "atlantis" || atlantis.Version != "6.15.0" || atlantis.IsFile {
		t.Errorf("unexpected atlantis chart: %+v", atlantis)
	}

	certManager, ok := charts["cert_manager"]
	if !ok || certManager.ChartName != "cert-manager" || certManager.Version != "v1.21.1" || !certManager.IsFile {
		t.Errorf("unexpected cert_manager chart: %+v", certManager)
	}
}

func TestParseKustomization(t *testing.T) {
	dir := t.TempDir()
	componentDir := filepath.Join(dir, "components", "my_comp")
	kustDir := filepath.Join(componentDir, "kustomize")
	if err := os.MkdirAll(kustDir, 0755); err != nil {
		t.Fatalf("failed to mkdir: %v", err)
	}

	kustFile := filepath.Join(kustDir, "kustomization.yaml")
	content := `
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  - deployment.yaml
`
	if err := os.WriteFile(kustFile, []byte(content), 0644); err != nil {
		t.Fatalf("failed to write file: %v", err)
	}

	ki, err := parseKustomization(componentDir, kustDir)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	if ki.Name != "base" {
		t.Errorf("expected name 'base', got %q", ki.Name)
	}
	if ki.IsComposition {
		t.Errorf("expected IsComposition false")
	}
}
