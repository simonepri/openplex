// Generate Bazel build rules for GitOps components, application definitions, and Helm manifests.

package gitops

import (
	"io/fs"
	"os"
	"path"
	"path/filepath"
	"slices"
	"sort"
	"strings"

	"openplex/bazel/gazelle/python"

	"github.com/bazel-contrib/bazel-gazelle/v2/rule"
	"github.com/bazelbuild/bazel-gazelle/language"
	bzl "github.com/bazelbuild/buildtools/build"
)

func (l *gitopsLang) GenerateRules(args language.GenerateArgs) language.GenerateResult {
	if args.Rel == "src/infra/argocd" {
		return language.GenerateResult{
			Gen:     []*rule.Rule{rule.NewRule("package_sources", "")},
			Imports: []interface{}{nil},
		}
	}
	if args.Rel == "src/infra/argocd/apps" {
		return l.generateAppsRules(args)
	}

	if !strings.HasPrefix(args.Rel, "src/infra/argocd/components/") {
		return language.GenerateResult{}
	}

	parts := strings.Split(args.Rel, "/")
	if len(parts) != 5 {
		return language.GenerateResult{}
	}
	componentName := parts[4]

	if l.vendorCharts == nil {
		modulePath := filepath.Join(args.Config.RepoRoot, "src/bazel/module_deps/vendor_helm_charts.MODULE.bazel")
		charts, err := parseVendorCharts(modulePath)
		if err == nil {
			l.vendorCharts = charts
		}
	}

	return l.generateComponentRules(args, componentName)
}

func (l *gitopsLang) generateAppsRules(args language.GenerateArgs) language.GenerateResult {
	appTemplates := rule.NewRule("source_set", "application_templates")
	appTemplates.SetAttr("kind", &bzl.Ident{Name: "HELM_TEMPLATE"})
	appTemplates.SetAttr("srcs", rule.GlobValue{
		Patterns: []string{"*.yaml"},
		Excludes: []string{"profiles.yaml"},
	})

	ps := rule.NewRule("package_sources", "")
	ps.SetAttr("exclude", []string{
		"cells.yaml",
		"ctrl.yaml",
		"projects.yaml",
		"teams.yaml",
	})

	return language.GenerateResult{
		Gen:     []*rule.Rule{appTemplates, ps},
		Imports: []interface{}{nil, nil},
	}
}

func (l *gitopsLang) generateComponentRules(args language.GenerateArgs, componentName string) language.GenerateResult {
	var rules []*rule.Rule
	if componentName == "vpa" {
		exported := rule.NewRule("exports_files", "")
		exported.SetAttr("srcs", []string{"helm/values.yaml"})
		exported.SetAttr("visibility", []string{"//src/infra/argocd/apps:__pkg__"})
		rules = append(rules, exported)
	}
	rules = append(rules, generateKustomizationRules(args.Dir)...)

	helmDir := filepath.Join(args.Dir, "helm")
	firstPartyHelm := generateFirstPartyHelmRules(args.Dir, helmDir, componentName)
	if len(firstPartyHelm) > 0 {
		rules = append(rules, firstPartyHelm...)
	} else {
		rules = append(rules, l.generateVendorHelmRules(args.Dir, helmDir, componentName)...)
	}

	shRules, shSrcs, shTests := generateShellRules(args.Dir, componentName)
	rules = append(rules, shRules...)

	pyRules, pySrcs, pyTests := generatePythonRules(args.Dir, componentName)
	rules = append(rules, pyRules...)

	rules = append(rules, generateSourceCoverage(args.Dir, helmDir, shSrcs, shTests, pySrcs, pyTests)...)

	imports := make([]interface{}, len(rules))
	for i, r := range rules {
		if r.Kind() == "py_library" || r.Kind() == "py_test" {
			imports[i] = python.ResolvedDependencies(r.AttrStrings("deps"))
		}
	}
	var empty []*rule.Rule
	if args.File != nil {
		for _, r := range args.File.Rules {
			if r.Kind() == "pdb_availability_check" {
				empty = append(empty, rule.NewRule("pdb_availability_check", r.Name()))
			}
		}
	}

	return language.GenerateResult{
		Gen:     rules,
		Empty:   empty,
		Imports: imports,
	}
}

func generateKustomizationRules(dir string) []*rule.Rule {
	var kustRoots []string
	filepath.WalkDir(dir, func(path string, d fs.DirEntry, err error) error {
		if err != nil {
			return nil
		}
		if !d.IsDir() && d.Name() == "kustomization.yaml" {
			kustRoots = append(kustRoots, filepath.Dir(path))
		}
		return nil
	})
	sort.Strings(kustRoots)

	var rules []*rule.Rule
	for _, root := range kustRoots {
		ki, err := parseKustomization(dir, root)
		if err != nil {
			continue
		}

		kr := rule.NewRule("kustomization", ki.Name)
		kr.SetAttr("srcs", rule.GlobValue{Patterns: kustomizationSourcePatterns(dir, ki)})
		kr.SetAttr("file", ki.File)
		rules = append(rules, kr)

		if !ki.IsComposition {
			renderRule := rule.NewRule("kustomized_resources", ki.Name+"_render")
			renderRule.SetAttr("kustomization", ":"+ki.Name)
			renderRule.SetAttr("visibility", []string{"//visibility:public"})
			rules = append(rules, renderRule)

			checkRule := rule.NewRule("manifest_checks", ki.Name+"_manifest")
			checkRule.SetAttr("rendered", ":"+ki.Name+"_render")
			rules = append(rules, checkRule)
		}
	}
	return rules
}

// kustomizationSourcePatterns covers the kustomization root plus every parent-relative reference that stays inside
// the component package, so overlays rebuild when the bases they reference change.
func kustomizationSourcePatterns(componentDir string, ki *kustInfo) []string {
	patterns := []string{ki.RelPath + "/**"}
	for _, ref := range ki.LocalReferences {
		target := path.Clean(path.Join(ki.RelPath, ref))
		if target == ".." || strings.HasPrefix(target, "../") {
			continue
		}
		info, err := os.Stat(filepath.Join(componentDir, filepath.FromSlash(target)))
		if err != nil {
			continue
		}
		if info.IsDir() {
			target += "/**"
		}
		if !slices.Contains(patterns, target) {
			patterns = append(patterns, target)
		}
	}
	return patterns
}

func generateFirstPartyHelmRules(dir, helmDir, componentName string) []*rule.Rule {
	chartFile := filepath.Join(helmDir, "Chart.yaml")
	if _, err := os.Stat(chartFile); err != nil {
		return nil
	}

	var rules []*rule.Rule
	hc := rule.NewRule("helm_chart", "chart")
	hc.SetAttr("chart", "helm/Chart.yaml")
	if _, err := os.Stat(filepath.Join(helmDir, "values.schema.json")); err == nil {
		hc.SetAttr("schema", "helm/values.schema.json")
	}

	var fileAssets []string
	filesDir := filepath.Join(helmDir, "files")
	filepath.WalkDir(filesDir, func(path string, d fs.DirEntry, err error) error {
		if err == nil && !d.IsDir() {
			rel, _ := filepath.Rel(helmDir, path)
			fileAssets = append(fileAssets, filepath.ToSlash(rel))
		}
		return nil
	})
	sort.Strings(fileAssets)
	if len(fileAssets) > 0 {
		hc.SetAttr("files", rule.GlobValue{Patterns: []string{"helm/files/**"}})
	}

	hc.SetAttr("templates", rule.GlobValue{Patterns: []string{"helm/templates/**"}, AllowEmpty: true})
	hc.SetAttr("values", "helm/values.yaml")
	rules = append(rules, hc)

	if len(fileAssets) > 0 {
		cft := rule.NewRule("helm_chart_files_test", "chart.files")
		cft.SetAttr("chart", ":chart")
		cft.SetAttr("files", fileAssets)
		rules = append(rules, cft)
	}

	rules = append(rules, generateFirstPartyLintRules(helmDir, componentName)...)

	gatewayRoutes := filepath.Join(dir, "validate-gateway-routes.sh")
	if _, err := os.Stat(gatewayRoutes); err == nil {
		gr := rule.NewRule("sh_test", "gateway-routes")
		gr.SetAttr("srcs", []string{"validate-gateway-routes.sh"})
		gr.SetAttr("args", []string{"$(location :helm_render)", "$(location :base_render)"})
		gr.SetAttr("data", []string{":base_render", ":helm_render"})
		rules = append(rules, gr)
	}
	return rules
}

func generateFirstPartyLintRules(helmDir, componentName string) []*rule.Rule {
	lintEntries, _ := os.ReadDir(helmDir)
	var lintFiles []string
	for _, e := range lintEntries {
		if !e.IsDir() && strings.HasPrefix(e.Name(), "lint-values") && strings.HasSuffix(e.Name(), ".yaml") {
			lintFiles = append(lintFiles, e.Name())
		}
	}
	sort.Strings(lintFiles)
	if len(lintFiles) == 0 {
		lintFiles = []string{""}
	}

	var rules []*rule.Rule
	for _, lf := range lintFiles {
		suffix, values := resolveLintFileSpec(componentName, lf)

		lintTest := rule.NewRule("helm_lint_test", "helm-lint"+suffix)
		lintTest.SetAttr("chart", ":chart")
		lintTest.SetAttr("opts", []string{"--strict"})
		if len(values) > 0 {
			lintTest.SetAttr("values", values)
		}
		rules = append(rules, lintTest)

		renderRule := rule.NewRule("helm_template", "helm_render"+suffix)
		renderRule.SetAttr("chart", ":chart")
		if len(values) > 0 {
			renderRule.SetAttr("values", values)
		}
		renderRule.SetAttr("visibility", []string{"//visibility:public"})
		rules = append(rules, renderRule)

		checkRule := rule.NewRule("manifest_checks", "helm"+suffix)
		checkRule.SetAttr("rendered", ":helm_render"+suffix)
		rules = append(rules, checkRule)
	}
	return rules
}

func resolveLintFileSpec(componentName, lf string) (string, []string) {
	if lf == "" {
		return "", nil
	}
	stem := strings.TrimSuffix(lf, ".yaml")
	suffix := strings.TrimPrefix(stem, "lint-values")
	var values []string
	if componentName == "routing_registry" {
		values = []string{"helm/routes.yaml"}
		if lf == "lint-values-local-identity.yaml" {
			values = append(values, "helm/lint-values-local.yaml")
		}
	}
	values = append(values, "helm/"+lf)
	return suffix, values
}

func (l *gitopsLang) generateVendorHelmRules(dir, helmDir, componentName string) []*rule.Rule {
	valuesFile := filepath.Join(helmDir, "values.yaml")
	if _, err := os.Stat(valuesFile); err != nil {
		return nil
	}
	info, hasVendorInfo := l.vendorCharts[componentName]
	if !hasVendorInfo {
		return nil
	}

	var rules []*rule.Rule
	externalChart := "@vendor_helm_" + componentName + "//:" + info.ChartName
	if info.IsFile {
		importRule := rule.NewRule("helm_import", "vendor_chart")
		importRule.SetAttr("chart", "@vendor_helm_"+componentName+"//file")
		importRule.SetAttr("version", info.Version)
		rules = append(rules, importRule)
		externalChart = ":vendor_chart"
	}

	for _, vs := range buildVendorValueSets(dir) {
		tr := rule.NewRule("helm_template", "vendor-helm-render"+vs.Suffix)
		tr.SetAttr("chart", externalChart)
		tr.SetAttr("opts", []string{"--include-crds", "--namespace", "manifest-checks"})
		tr.SetAttr("values", vs.Values)
		if (componentName == "gpu_operator" && (vs.Suffix == "-provider-aws" || vs.Suffix == "-provider-gcp")) ||
			componentName == "dragonfly_manager" || componentName == "dragonfly_peer" {
			tr.SetAttr("visibility", []string{"//src/infra/argocd/apps:__pkg__"})
		}
		rules = append(rules, tr)

		checkRule := rule.NewRule("manifest_checks", "vendor-helm"+vs.Suffix)
		checkRule.SetAttr("rendered", ":vendor-helm-render"+vs.Suffix)
		checkRule.SetAttr("kube_lint_config", "//src/bazel/rules/kubernetes_manifests:kube-linter-vendor.yaml")
		rules = append(rules, checkRule)
	}
	return rules
}

func generateShellRules(dir, componentName string) ([]*rule.Rule, []string, []string) {
	var shellSources, shellTests []string
	filepath.WalkDir(dir, func(path string, d fs.DirEntry, err error) error {
		if err != nil || d.IsDir() {
			return nil
		}
		if strings.HasSuffix(d.Name(), ".sh") {
			rel, _ := filepath.Rel(dir, path)
			rel = filepath.ToSlash(rel)
			if strings.HasSuffix(d.Name(), "_test.sh") {
				shellTests = append(shellTests, rel)
			} else {
				shellSources = append(shellSources, rel)
			}
		}
		return nil
	})
	sort.Strings(shellSources)
	sort.Strings(shellTests)

	var rules []*rule.Rule
	if len(shellSources) > 0 {
		sl := rule.NewRule("sh_library", "shell")
		sl.SetAttr("srcs", shellSources)
		rules = append(rules, sl)
	}

	for _, st := range shellTests {
		stem := strings.TrimSuffix(filepath.Base(st), ".sh")
		subject := strings.TrimSuffix(st, "_test.sh") + ".sh"
		stRule := rule.NewRule("sh_test", stem)
		stRule.SetAttr("srcs", []string{st})
		if componentName == "karpenter_aws" && stem == "stargz-bootstrap_test" {
			stRule.SetAttr("args", []string{
				"$(location :overlay-cells_render)",
				"$(location bootstrap/stargz-bootstrap.sh)",
				"$(location bootstrap/stargz-config.toml)",
				"$(location bootstrap/stargz-snapshotter.service)",
				"$(location bootstrap/containerd-stargz.conf)",
				"$(location bootstrap/stargz-node-config.yaml)",
				"$(location //src/bazel/tools:yq)",
				"$(location bootstrap/dragonfly-mirror.toml)",
				"$(location bootstrap/dragonfly-prepull.service)",
				"$(location bootstrap/setup-dragonfly-prepull.sh)",
			})
			stRule.SetAttr("data", []string{
				":overlay-cells_render",
				"bootstrap/containerd-stargz.conf",
				"bootstrap/dragonfly-mirror.toml",
				"bootstrap/dragonfly-prepull.service",
				"bootstrap/setup-dragonfly-prepull.sh",
				"bootstrap/stargz-bootstrap.sh",
				"bootstrap/stargz-config.toml",
				"bootstrap/stargz-node-config.yaml",
				"bootstrap/stargz-snapshotter.service",
				"//src/bazel/tools:yq",
			})
		} else {
			stRule.SetAttr("args", []string{"$(location " + subject + ")"})
			stRule.SetAttr("data", []string{subject})
		}
		rules = append(rules, stRule)
	}
	return rules, shellSources, shellTests
}

func generatePythonRules(dir, componentName string) ([]*rule.Rule, []string, []string) {
	var pySources, pyTests []string
	filepath.WalkDir(dir, func(path string, d fs.DirEntry, err error) error {
		if err != nil || d.IsDir() {
			return nil
		}
		if strings.HasSuffix(d.Name(), ".py") {
			rel, _ := filepath.Rel(dir, path)
			rel = filepath.ToSlash(rel)
			if strings.HasSuffix(d.Name(), "_test.py") {
				pyTests = append(pyTests, rel)
			} else {
				pySources = append(pySources, rel)
			}
		}
		return nil
	})
	sort.Strings(pySources)
	sort.Strings(pyTests)

	var rules []*rule.Rule
	if len(pySources) > 0 {
		pl := rule.NewRule("py_library", "python")
		pl.SetAttr("srcs", pySources)
		var pyImports []string
		seenImports := make(map[string]bool)
		for _, s := range pySources {
			d := filepath.Dir(s)
			if d != "" && !seenImports[d] {
				seenImports[d] = true
				pyImports = append(pyImports, d)
			}
		}
		sort.Strings(pyImports)
		pl.SetAttr("imports", pyImports)
		if componentName == "coder" {
			pl.SetAttr("visibility", []string{"//src/infra/definitions/conformance:__pkg__"})
		}
		rules = append(rules, pl)
	}

	for _, pt := range pyTests {
		rules = append(rules, generatePyTestRule(componentName, dir, pt, pySources))
	}
	return rules, pySources, pyTests
}

func generatePyTestRule(componentName, dir, pt string, pySources []string) *rule.Rule {
	stem := strings.TrimSuffix(filepath.Base(pt), ".py")
	ptRule := rule.NewRule("py_test", stem)
	ptRule.SetAttr("srcs", []string{pt})
	if (componentName == "trivy_operator" && stem == "scan_security_test") ||
		(componentName == "otel_collector" && stem == "scrape_test") {
		ptRule.SetAttr("args", []string{"$(location :vendor-helm-render)"})
		ptRule.SetAttr("data", []string{":vendor-helm-render"})
		ptRule.SetAttr("deps", []string{"@dev_python_deps//pyyaml"})
	}
	if componentName == "gpu_operator" && stem == "cleanup_test" {
		ptRule.SetAttr("args", []string{"$(location :base_render)", "$(location :vendor-helm-render)"})
		ptRule.SetAttr("data", []string{":base_render", ":vendor-helm-render"})
		ptRule.SetAttr("deps", []string{"@dev_python_deps//pyyaml"})
	}
	if componentName == "kueue_admission" && stem == "dev_queue_test" {
		ptRule.SetAttr("args", []string{"$(location :helm_render)", "$(location :helm_render-dev-queue-disabled)"})
		ptRule.SetAttr("data", []string{":helm_render", ":helm_render-dev-queue-disabled"})
		ptRule.SetAttr("deps", []string{"@dev_python_deps//pyyaml"})
	}
	if componentName == "valkey_operator" && stem == "cache_scheduling_test" {
		ptRule.SetAttr("args", []string{"$(location :base_render)", "$(location //src/infra/argocd/components/torch_compile_cache:base_render)"})
		ptRule.SetAttr("data", []string{":base_render", "//src/infra/argocd/components/torch_compile_cache:base_render"})
		ptRule.SetAttr("deps", []string{"@dev_python_deps//pyyaml"})
	}
	if len(pySources) > 0 {
		ptRule.SetAttr("deps", []string{":python"})
	}
	if componentName == "tailscale_access" && stem == "local_router_test" {
		ptRule.SetAttr("args", []string{"$(location :helm_render-local)", "$(location :helm_render-local-egress)"})
		ptRule.SetAttr("data", []string{":helm_render-local", ":helm_render-local-egress"})
		ptRule.SetAttr("deps", []string{"@dev_python_deps//pyyaml"})
	}
	if (componentName == "k8s_cleaner" && stem == "pv_reclaimer_test") ||
		(componentName == "team_lane" && stem == "registered_clusters_test") {
		ptRule.SetAttr("args", []string{"$(locations @rules_helm//helm:current_toolchain)", "$(location helm/Chart.yaml)"})
		data := []string{"@rules_helm//helm:current_toolchain"}
		filepath.WalkDir(filepath.Join(dir, "helm"), func(path string, d fs.DirEntry, err error) error {
			if err == nil && !d.IsDir() {
				rel, _ := filepath.Rel(dir, path)
				data = append(data, filepath.ToSlash(rel))
			}
			return nil
		})
		sort.Strings(data)
		ptRule.SetAttr("data", data)
		if componentName == "k8s_cleaner" {
			ptRule.SetAttr("deps", []string{"@dev_python_deps//jsonschema", "@dev_python_deps//pyyaml"})
		} else {
			ptRule.SetAttr("deps", []string{"@dev_python_deps//pyyaml"})
		}
	}
	if componentName == "k8s_cleaner" && stem == "corrupt_image_cleaner_test" {
		ptRule.SetAttr("args", []string{"$(locations @rules_helm//helm:current_toolchain)", "$(location @vendor_helm_k8s_cleaner//:k8s-cleaner)", "$(location kustomize/cleaner-corrupt-image.yaml)"})
		ptRule.SetAttr("data", []string{"kustomize/cleaner-corrupt-image.yaml", "@rules_helm//helm:current_toolchain", "@vendor_helm_k8s_cleaner//:k8s-cleaner"})
		ptRule.SetAttr("deps", []string{"@dev_python_deps//jsonschema", "@dev_python_deps//pyyaml"})
	}
	if componentName == "node_problem_detector" && stem == "node_problem_detector_test" {
		ptRule.SetAttr("data", rule.GlobValue{Patterns: []string{"kustomize/**"}})
		ptRule.SetAttr("deps", []string{"@dev_python_deps//pyyaml"})
	}
	if componentName == "cloud_telemetry" && stem == "pipeline_test" {
		ptRule.SetAttr("data", []string{"helm/values.yaml"})
		ptRule.SetAttr("deps", []string{"@dev_python_deps//pyyaml"})
	}
	return ptRule
}

func generateSourceCoverage(dir, helmDir string, shellSources, shellTests, pySources, pyTests []string) []*rule.Rule {
	var rules []*rule.Rule
	hasTemplates := false
	if _, err := os.Stat(filepath.Join(helmDir, "templates")); err == nil {
		hasTemplates = true
		st := rule.NewRule("source_set", "helm_templates")
		st.SetAttr("kind", &bzl.Ident{Name: "HELM_TEMPLATE"})
		st.SetAttr("srcs", rule.GlobValue{Patterns: []string{"helm/templates/**"}})
		rules = append(rules, st)
	}

	var generatedValues []string
	cellValues, _ := filepath.Glob(filepath.Join(helmDir, "lint-values-cell-*.yaml"))
	for _, cv := range cellValues {
		rel, _ := filepath.Rel(dir, cv)
		generatedValues = append(generatedValues, filepath.ToSlash(rel))
	}
	sort.Strings(generatedValues)
	if len(generatedValues) > 0 {
		gv := rule.NewRule("source_set", "generated_values")
		gv.SetAttr("kind", &bzl.Ident{Name: "GENERATED"})
		gv.SetAttr("srcs", generatedValues)
		rules = append(rules, gv)
	}

	var excludes []string
	if len(generatedValues) > 0 {
		excludes = append(excludes, generatedValues...)
	}
	if hasTemplates {
		excludes = append(excludes, "helm/templates/**")
	}
	if len(shellSources) > 0 || len(shellTests) > 0 {
		excludes = append(excludes, "**/*.sh")
	}
	if len(pySources) > 0 || len(pyTests) > 0 {
		excludes = append(excludes, "**/*.py")
	}

	ps := rule.NewRule("package_sources", "")
	if len(excludes) > 0 {
		ps.SetAttr("exclude", excludes)
	}
	rules = append(rules, ps)
	return rules
}
