// Test that only components a topology calls skip their own validate test.

package opentofu

import (
	"os"
	"path/filepath"
	"testing"

	"github.com/bazelbuild/bazel-gazelle/config"
	"github.com/bazelbuild/bazel-gazelle/language"
)

func writeFile(t *testing.T, path string, content string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatalf("failed to create dir: %v", err)
	}
	if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
		t.Fatalf("failed to write %s: %v", path, err)
	}
}

func TestGenerateRulesSkipsValidationOnlyForTopologyCalledComponents(t *testing.T) {
	repoRoot := t.TempDir()
	writeFile(t, filepath.Join(repoRoot, "src/infra/terraform/topologies/ctrl/aws/main.tf"), `
module "dns" {
  source = "../../../components/dns/aws"
}
`)
	for _, rel := range []string{
		"src/infra/terraform/components/dns/aws",
		"src/infra/terraform/components/dns/gcp",
	} {
		writeFile(t, filepath.Join(repoRoot, rel, "main.tf"), "")
	}

	cases := map[string]bool{
		"src/infra/terraform/components/dns/aws": true,
		"src/infra/terraform/components/dns/gcp": false,
	}
	lang := NewLanguage()
	for rel, wantSkip := range cases {
		result := lang.GenerateRules(language.GenerateArgs{
			Config:       &config.Config{RepoRoot: repoRoot},
			Dir:          filepath.Join(repoRoot, rel),
			Rel:          rel,
			RegularFiles: []string{"main.tf"},
		})
		if len(result.Gen) == 0 {
			t.Fatalf("%s: no rules generated", rel)
		}
		gotSkip := result.Gen[0].AttrBool("skip_validation")
		if gotSkip != wantSkip {
			t.Errorf("%s: skip_validation = %v, want %v", rel, gotSkip, wantSkip)
		}
	}
}
