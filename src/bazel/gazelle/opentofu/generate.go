// Generate Bazel build rules and component targets from OpenTofu module definitions.

package opentofu

import (
	"log"
	"path/filepath"
	"strings"

	"github.com/bazel-contrib/bazel-gazelle/v2/rule"
	"github.com/bazelbuild/bazel-gazelle/language"
)

var componentData = map[string][]string{
	"container_registry": {
		"//src/infra/images:workload-images.json",
		"//src/infra/definitions/workspaces/templates/dev:workspace-images.json",
	},
	"tailscale_tailnet": {
		"//src/infra/argocd/components/tailscale_access:cloud-policy.hujson",
	},
}

func (l *opentofuLang) GenerateRules(args language.GenerateArgs) language.GenerateResult {
	if !strings.HasPrefix(args.Rel, "src/infra/terraform/components") {
		return language.GenerateResult{}
	}

	var hasTf bool
	for _, f := range args.RegularFiles {
		if strings.HasSuffix(f, ".tf") {
			hasTf = true
			break
		}
	}
	if !hasTf {
		return language.GenerateResult{}
	}

	name := filepath.Base(args.Rel)
	tfRule := rule.NewRule("tf_module", name)

	providers, err := parseProviders(filepath.Join(args.Dir, "versions.tf"))
	if err == nil && len(providers) > 0 {
		tfRule.SetAttr("providers", providers)
	}

	deps, err := parseModuleDependencies(args.Dir, args.Config.RepoRoot, args.RegularFiles)
	if err == nil && len(deps) > 0 {
		tfRule.SetAttr("deps", deps)
	}

	if data, ok := componentData[name]; ok {
		tfRule.SetAttr("data", data)
	}

	called, err := topologyCalledModules(args.Config.RepoRoot)
	if err != nil {
		log.Printf("opentofu: keeping validate for %s: %v", args.Rel, err)
	} else if called["//"+args.Rel] {
		tfRule.SetAttr("skip_validation", true)
	}

	psRule := rule.NewRule("package_sources", "")

	return language.GenerateResult{
		Gen:     []*rule.Rule{tfRule, psRule},
		Imports: []interface{}{nil, nil},
	}
}
