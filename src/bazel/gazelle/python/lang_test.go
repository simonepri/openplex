// Test Python Gazelle extension behavior for extracting module imports and generating Bazel requirements.

package python_test

import (
	"os"
	"os/exec"
	"path/filepath"
	"reflect"
	"testing"

	"github.com/bazel-contrib/bazel-gazelle/v2/rule"
	"github.com/bazelbuild/rules_go/go/tools/bazel"
)

func TestPythonDependenciesAcrossGenerators(t *testing.T) {
	bin, ok := bazel.FindBinary("src/bazel/gazelle/python", "gazelle_test_bin")
	if !ok {
		t.Fatal("Gazelle test binary is missing")
	}
	dir := t.TempDir()
	files := map[string]string{
		"MODULE.bazel": "module(name = \"fixture\")\n",
		"BUILD.bazel": "# gazelle:python_generation_mode file\n" +
			"# gazelle:python_default_visibility NONE\n" +
			"# gazelle:exclude src/infra/argocd/components/coder/kustomize\n",
		"src/infra/terraform/lifecycle/cluster_up.py":                        "def main():\n    pass\n\nif __name__ == \"__main__\":\n    main()\n",
		"src/infra/terraform/lifecycle/cluster_down.py":                      "def main():\n    pass\n\nif __name__ == \"__main__\":\n    main()\n",
		"src/infra/terraform/components/floci_runtime/upgrade.py":            "def main():\n    pass\n\nif __name__ == \"__main__\":\n    main()\n",
		"src/infra/terraform/components/floci_runtime/upgrade_test.py":       "from src.infra.terraform.components.floci_runtime import upgrade\n",
		"src/infra/terraform/components/floci_runtime/helper.py":             "def helper():\n    pass\n",
		"src/infra/terraform/components/floci_runtime/helper_test.py":        "from src.infra.terraform.components.floci_runtime import helper\n",
		"src/infra/argocd/components/coder/kustomize/scripts/worker.py":      "def worker():\n    pass\n",
		"src/infra/argocd/components/coder/kustomize/scripts/worker_test.py": "import worker\n",
	}
	for name, content := range files {
		path := filepath.Join(dir, name)
		if err := os.MkdirAll(filepath.Dir(path), 0755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(path, []byte(content), 0644); err != nil {
			t.Fatal(err)
		}
	}

	previous := make(map[string]string)
	for generation := 0; generation < 2; generation++ {
		cmd := exec.Command(bin, "-repo_root", dir, dir)
		cmd.Dir = dir
		if output, err := cmd.CombinedOutput(); err != nil {
			t.Fatalf("Gazelle failed: %v\n%s", err, output)
		}
		for _, tc := range []struct {
			pkg, target, attr, dep string
		}{
			{"src/infra/terraform/lifecycle", "cluster_up", "data", "//src/infra/images:seed"},
			{"src/infra/terraform/lifecycle", "cluster_down", "data", ""},
			{"src/infra/terraform/components/floci_runtime", "upgrade_test", "deps", ":upgrade"},
			{"src/infra/terraform/components/floci_runtime", "helper_test", "deps", ":helper"},
			{"src/infra/argocd/components/coder", "worker_test", "deps", ":python"},
		} {
			path := filepath.Join(dir, tc.pkg, "BUILD.bazel")
			build, err := rule.LoadFile(path, tc.pkg)
			if err != nil {
				t.Fatal(err)
			}
			found := false
			for _, r := range build.Rules {
				if r.Name() == tc.target {
					found = true
					want := []string{tc.dep}
					if tc.dep == "" {
						want = nil
					}
					if got := r.AttrStrings(tc.attr); !reflect.DeepEqual(got, want) {
						t.Errorf("%s:%s %s = %v; want %v", tc.pkg, tc.target, tc.attr, got, want)
					}
				}
			}
			if !found {
				t.Errorf("%s:%s was not generated", tc.pkg, tc.target)
			}
			content, err := os.ReadFile(path)
			if err != nil {
				t.Fatal(err)
			}
			if generation > 0 && previous[path] != string(content) {
				t.Errorf("repeated generation changed %s", path)
			}
			previous[path] = string(content)
		}
	}
}
