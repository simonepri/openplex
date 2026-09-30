// Resolve Python package requirements and integrate inferred dependencies with GitOps component targets.

package python

import (
	"strings"

	"github.com/bazel-contrib/bazel-gazelle/v2/label"
	"github.com/bazel-contrib/bazel-gazelle/v2/rule"
	gazellepython "github.com/bazel-contrib/rules_python/gazelle/python"
	"github.com/bazelbuild/bazel-gazelle/config"
	"github.com/bazelbuild/bazel-gazelle/language"
	"github.com/bazelbuild/bazel-gazelle/repo"
	"github.com/bazelbuild/bazel-gazelle/resolve"
)

type pythonLang struct {
	gazellepython.Python
}

// ResolvedDependencies carries dependency labels supplied by another generator.
type ResolvedDependencies []string

// NewLanguage returns Python generation with support for explicit GitOps dependencies.
func NewLanguage() language.Language {
	return &pythonLang{}
}

func (l *pythonLang) GenerateRules(args language.GenerateArgs) language.GenerateResult {
	if args.Rel == "src/infra/argocd/apps" ||
		strings.HasPrefix(args.Rel, "src/infra/argocd/components") ||
		strings.HasPrefix(args.Rel, "src/infra/definitions/workspaces") ||
		strings.HasPrefix(args.Rel, "src/infra/definitions/conformance") {
		return language.GenerateResult{}
	}
	result := l.Python.GenerateRules(args)
	if args.Rel == "src/infra/terraform/lifecycle" {
		for _, target := range result.Gen {
			if target.Name() == "cluster_up" {
				target.SetAttr("data", []string{"//src/infra/images:seed"})
			}
		}
	}
	return result
}

func (l *pythonLang) Resolve(c *config.Config, ix *resolve.RuleIndex, rc *repo.RemoteCache, r *rule.Rule, imports interface{}, from label.Label) {
	if deps, ok := imports.(ResolvedDependencies); ok {
		if len(deps) > 0 {
			r.SetAttr("deps", []string(deps))
		} else {
			r.DelAttr("deps")
		}
		return
	}
	l.Python.Resolve(c, ix, rc, r, imports, from)
}
