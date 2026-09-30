// Implement the Gazelle language extension interface for GitOps manifests and component rules.

package gitops

import (
	"github.com/bazel-contrib/bazel-gazelle/v2/rule"
	"github.com/bazelbuild/bazel-gazelle/language"
)

type gitopsLang struct {
	language.BaseLang
	vendorCharts map[string]vendorChartInfo
}

// NewLanguage returns a new gitops language extension.
func NewLanguage() language.Language {
	return &gitopsLang{}
}

func (l *gitopsLang) Name() string {
	return "gitops"
}

func (l *gitopsLang) Kinds() map[string]rule.KindInfo {
	return map[string]rule.KindInfo{
		"exports_files": {
			MatchAny:       true,
			NonEmptyAttrs:  map[string]bool{"srcs": true},
			MergeableAttrs: map[string]bool{"srcs": true, "visibility": true},
		},
		"kustomization": {
			NonEmptyAttrs: map[string]bool{"name": true},
			MergeableAttrs: map[string]bool{
				"file":       true,
				"deps":       true,
				"visibility": true,
			},
		},
		"kustomized_resources": {
			NonEmptyAttrs: map[string]bool{"name": true},
			MergeableAttrs: map[string]bool{
				"kustomization": true,
				"visibility":    true,
			},
		},
		"manifest_checks": {
			NonEmptyAttrs: map[string]bool{"name": true},
			MergeableAttrs: map[string]bool{
				"rendered": true,
			},
		},
		"helm_chart": {
			NonEmptyAttrs: map[string]bool{"name": true},
			MergeableAttrs: map[string]bool{
				"chart":  true,
				"schema": true,
				"files":  true,
				"values": true,
			},
		},
		"helm_chart_files_test": {
			NonEmptyAttrs: map[string]bool{"name": true},
			MergeableAttrs: map[string]bool{
				"chart": true,
				"files": true,
			},
		},
		"helm_lint_test": {
			NonEmptyAttrs: map[string]bool{"name": true},
			MergeableAttrs: map[string]bool{
				"chart":  true,
				"opts":   true,
				"values": true,
			},
		},
		"helm_template": {
			NonEmptyAttrs: map[string]bool{"name": true},
			MergeableAttrs: map[string]bool{
				"chart":      true,
				"opts":       true,
				"values":     true,
				"visibility": true,
			},
		},
		"helm_import": {
			NonEmptyAttrs: map[string]bool{"name": true},
			MergeableAttrs: map[string]bool{
				"chart":   true,
				"version": true,
			},
		},
		"pdb_availability_check": {
			NonEmptyAttrs: map[string]bool{"rendered": true},
			MergeableAttrs: map[string]bool{
				"rendered": true,
			},
		},
		"sh_library": {
			NonEmptyAttrs: map[string]bool{"name": true},
			MergeableAttrs: map[string]bool{
				"srcs": true,
			},
		},
		"sh_test": {
			NonEmptyAttrs: map[string]bool{"name": true},
			MergeableAttrs: map[string]bool{
				"srcs": true,
				"args": true,
				"data": true,
			},
		},
		"source_set": {
			NonEmptyAttrs:  map[string]bool{"name": true},
			MergeableAttrs: map[string]bool{"kind": true},
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

func (l *gitopsLang) ApparentLoads(moduleToApparentName func(string) string) []rule.LoadInfo {
	return []rule.LoadInfo{
		{
			Name: "@rules_kustomize//kustomize:kustomize.bzl",
			Symbols: []string{
				"kustomization",
				"kustomized_resources",
			},
		},
		{
			Name: "@rules_helm//helm:defs.bzl",
			Symbols: []string{
				"helm_chart",
				"helm_chart_files_test",
				"helm_import",
				"helm_lint_test",
				"helm_template",
			},
		},
		{
			Name: "@rules_shell//shell:sh_library.bzl",
			Symbols: []string{
				"sh_library",
			},
		},
		{
			Name: "@rules_shell//shell:sh_test.bzl",
			Symbols: []string{
				"sh_test",
			},
		},
		{
			Name: "@rules_python//python:defs.bzl",
			Symbols: []string{
				"py_library",
				"py_test",
			},
		},
		{
			Name: "//src/bazel/rules/kubernetes_manifests:defs.bzl",
			Symbols: []string{
				"manifest_checks",
				"pdb_availability_check",
			},
		},
		{
			Name: "//src/bazel/rules/build_graph_coverage:defs.bzl",
			Symbols: []string{
				"GENERATED",
				"HELM_TEMPLATE",
				"package_sources",
				"source_set",
			},
		},
	}
}

func (l *gitopsLang) Loads() []rule.LoadInfo {
	return l.ApparentLoads(nil)
}
