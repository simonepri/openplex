// Implement the Gazelle language interface and register OpenTofu rule kinds and configuration directives.

package opentofu

import (
	"github.com/bazel-contrib/bazel-gazelle/v2/rule"
	"github.com/bazelbuild/bazel-gazelle/language"
)

type opentofuLang struct {
	language.BaseLang
}

// NewLanguage returns a new opentofu language extension.
func NewLanguage() language.Language {
	return &opentofuLang{}
}

func (l *opentofuLang) Name() string {
	return "opentofu"
}

func (l *opentofuLang) Kinds() map[string]rule.KindInfo {
	return map[string]rule.KindInfo{
		"tf_module": {
			NonEmptyAttrs: map[string]bool{
				"name": true,
			},
			MergeableAttrs: map[string]bool{
				"deps":            true,
				"providers":       true,
				"data":            true,
				"skip_validation": true,
			},
			ResolveAttrs: map[string]bool{
				"deps": true,
			},
		},
		"package_sources": {
			MatchAny:      true,
			NonEmptyAttrs: map[string]bool{},
			MergeableAttrs: map[string]bool{
				"exclude":     true,
				"extra_globs": true,
			},
		},
	}
}

func (l *opentofuLang) ApparentLoads(moduleToApparentName func(string) string) []rule.LoadInfo {
	return []rule.LoadInfo{
		{
			Name:    "@rules_tf//tf:def.bzl",
			Symbols: []string{"tf_module"},
		},
		{
			Name:    "//src/bazel/rules/build_graph_coverage:defs.bzl",
			Symbols: []string{"package_sources"},
		},
	}
}

func (l *opentofuLang) Loads() []rule.LoadInfo {
	return l.ApparentLoads(nil)
}
