// Test that fresh Helm components receive typed source coverage and resolvable constant imports.

package gitops_test

import (
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"

	"github.com/bazel-contrib/bazel-gazelle/v2/rule"
	bzl "github.com/bazelbuild/buildtools/build"
	"github.com/bazelbuild/rules_go/go/tools/bazel"
)

func TestFreshHelmComponentHasTypedSourceCoverage(t *testing.T) {
	bin, ok := bazel.FindBinary("src/bazel/gazelle/gitops", "gazelle_test_bin")
	if !ok {
		t.Fatal("Gazelle test binary is missing")
	}
	dir := t.TempDir()
	pkg := "src/infra/argocd/components/fresh_chart"
	for name, content := range map[string]string{
		"MODULE.bazel":                           "module(name = \"fixture\")\n",
		pkg + "/helm/Chart.yaml":                 "apiVersion: v2\nname: fresh-chart\nversion: 0.1.0\n",
		pkg + "/helm/values.yaml":                "{}\n",
		pkg + "/helm/templates/config.yaml":      "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: {{ .Release.Name }}\n",
		pkg + "/helm/lint-values-cell-test.yaml": "{}\n",
	} {
		path := filepath.Join(dir, name)
		if err := os.MkdirAll(filepath.Dir(path), 0755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(path, []byte(content), 0644); err != nil {
			t.Fatal(err)
		}
	}
	var previous string
	for generation := 0; generation < 2; generation++ {
		cmd := exec.Command(bin, "-repo_root", dir, dir)
		cmd.Dir = dir
		if output, err := cmd.CombinedOutput(); err != nil {
			t.Fatalf("Gazelle failed: %v\n%s", err, output)
		}
		path := filepath.Join(dir, pkg, "BUILD.bazel")
		file, err := rule.LoadFile(path, pkg)
		if err != nil {
			t.Fatal(err)
		}
		for target, kind := range map[string]string{
			"helm_templates":   "HELM_TEMPLATE",
			"generated_values": "GENERATED",
		} {
			found := false
			for _, r := range file.Rules {
				if r.Name() == target {
					found = true
					got, ok := r.Attr("kind").(*bzl.Ident)
					if !ok || got.Name != kind {
						t.Errorf("%s kind = %v; want %s", target, r.Attr("kind"), kind)
					}
				}
			}
			if !found {
				t.Errorf("%s was not generated", target)
			}
			loaded := false
			for _, load := range file.Loads {
				if load.Name() == "//src/bazel/rules/build_graph_coverage:defs.bzl" {
					for _, symbol := range load.Symbols() {
						loaded = loaded || symbol == kind
					}
				}
			}
			if !loaded {
				t.Errorf("%s was not imported", kind)
			}
		}
		content, err := os.ReadFile(path)
		if err != nil {
			t.Fatal(err)
		}
		if generation > 0 && strings.TrimSpace(previous) != strings.TrimSpace(string(content)) {
			t.Error("repeated generation changed the component BUILD file")
		}
		previous = string(content)
	}
}
