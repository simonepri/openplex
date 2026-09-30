// Parse Kustomization manifests, Helm charts, and vendor chart imports to resolve GitOps dependencies.

package gitops

import (
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
)

type vendorChartInfo struct {
	Component string
	ChartName string
	Version   string
	IsFile    bool
}

var (
	fileVersionRe   = regexp.MustCompile(`(?:_helm_|-v?)([\d\.]+(?:-[a-z0-9\.]+)?)\.tgz$`)
	fileChartNameRe = regexp.MustCompile(`^([a-zA-Z0-9_-]+?)(?:_helm_|-v?[\d\.]+)`)
	kustReferenceRe = regexp.MustCompile(`(?m)^\s*-\s+['"]?(\.\.[^'"\s#]*)`)
	kindComponentRe = regexp.MustCompile(`(?m)^kind:\s*Component\s*$`)
	resourcesKeyRe  = regexp.MustCompile(`(?m)^resources:\s*$`)
)

func parseVendorCharts(modulePath string) (map[string]vendorChartInfo, error) {
	content, err := os.ReadFile(modulePath)
	if err != nil {
		return nil, err
	}

	result := make(map[string]vendorChartInfo)
	text := string(content)

	chartBlocks := strings.Split(text, "vendor_helm_chart(")
	for _, block := range chartBlocks[1:] {
		compMatch := regexp.MustCompile(`name\s*=\s*"vendor_helm_([^"]+)"`).FindStringSubmatch(block)
		if len(compMatch) < 2 {
			continue
		}
		comp := compMatch[1]

		chartName := comp
		version := ""

		if cm := regexp.MustCompile(`chart_name\s*=\s*"([^"]+)"`).FindStringSubmatch(block); len(cm) > 1 {
			chartName = cm[1]
		}
		if vm := regexp.MustCompile(`version\s*=\s*"([^"]+)"`).FindStringSubmatch(block); len(vm) > 1 {
			version = vm[1]
		}
		if um := regexp.MustCompile(`url\s*=\s*"oci://[^"]+/([^/:]+):([^"]+)"`).FindStringSubmatch(block); len(um) > 2 {
			chartName = um[1]
			version = um[2]
		}

		result[comp] = vendorChartInfo{
			Component: comp,
			ChartName: chartName,
			Version:   version,
			IsFile:    false,
		}
	}

	fileBlocks := strings.Split(text, "vendor_helm_file(")
	for _, block := range fileBlocks[1:] {
		compMatch := regexp.MustCompile(`name\s*=\s*"vendor_helm_([^"]+)"`).FindStringSubmatch(block)
		if len(compMatch) < 2 {
			continue
		}
		comp := compMatch[1]

		filename := ""
		if fm := regexp.MustCompile(`downloaded_file_path\s*=\s*"([^"]+)"`).FindStringSubmatch(block); len(fm) > 1 {
			filename = fm[1]
		}

		version := ""
		if vm := fileVersionRe.FindStringSubmatch(filename); len(vm) > 1 {
			version = vm[1]
			if strings.HasPrefix(filename, "cert-manager-v") || strings.HasPrefix(filename, "gpu-operator-v") {
				version = "v" + version
			}
		}

		chart := comp
		if cm := fileChartNameRe.FindStringSubmatch(filename); len(cm) > 1 {
			chart = cm[1]
		}

		result[comp] = vendorChartInfo{
			Component: comp,
			ChartName: chart,
			Version:   version,
			IsFile:    true,
		}
	}

	return result, nil
}

type kustInfo struct {
	Name            string
	RelPath         string
	File            string
	IsComposition   bool
	LocalReferences []string
}

func parseKustomization(componentDir, rootDir string) (*kustInfo, error) {
	kustFile := filepath.Join(rootDir, "kustomization.yaml")
	content, err := os.ReadFile(kustFile)
	if err != nil {
		return nil, err
	}

	rel, err := filepath.Rel(componentDir, rootDir)
	if err != nil {
		return nil, err
	}
	rel = filepath.ToSlash(rel)

	cleanRel := strings.TrimPrefix(rel, "kustomize")
	cleanRel = strings.TrimPrefix(cleanRel, "/")
	name := "base"
	if cleanRel != "" {
		name = strings.ReplaceAll(cleanRel, "overlays/", "overlay-")
		name = strings.ReplaceAll(name, "/", "-")
	}

	isComp := kindComponentRe.Match(content) && !resourcesKeyRe.Match(content)

	var refs []string
	for _, m := range kustReferenceRe.FindAllSubmatch(content, -1) {
		refs = append(refs, string(m[1]))
	}

	return &kustInfo{
		Name:            name,
		RelPath:         rel,
		File:            filepath.ToSlash(filepath.Join(rel, "kustomization.yaml")),
		IsComposition:   isComp,
		LocalReferences: refs,
	}, nil
}

type valueSet struct {
	Suffix string
	Values []string
}

func buildVendorValueSets(componentDir string) []valueSet {
	helmDir := filepath.Join(componentDir, "helm")
	renderValues := []string{"helm/values.yaml"}
	if _, err := os.Stat(filepath.Join(helmDir, "lint-values.yaml")); err == nil {
		renderValues = append(renderValues, "helm/lint-values.yaml")
	}

	valueSets := []valueSet{
		{Suffix: "", Values: renderValues},
	}

	providers := listYamlStems(filepath.Join(helmDir, "providers"))
	profiles := listYamlStems(filepath.Join(helmDir, "profiles"))
	profileProviders := listYamlStems(filepath.Join(helmDir, "profile_providers"))
	roles := listYamlStems(filepath.Join(helmDir, "roles"))

	for _, p := range providers {
		valueSets = append(valueSets, valueSet{
			Suffix: "-provider-" + p,
			Values: append(slicesClone(renderValues), "helm/providers/"+p+".yaml"),
		})
	}

	seenProviders := make(map[string]bool)
	for _, p := range providers {
		seenProviders[p] = true
	}
	for _, pp := range profileProviders {
		if !seenProviders[pp] {
			valueSets = append(valueSets, valueSet{
				Suffix: "-provider-" + pp,
				Values: slicesClone(renderValues),
			})
		}
	}

	for _, r := range roles {
		roleValues := append(slicesClone(renderValues), "helm/roles/"+r+".yaml")
		valueSets = append(valueSets, valueSet{
			Suffix: "-role-" + r,
			Values: roleValues,
		})
		for _, p := range providers {
			comboValues := append(slicesClone(renderValues), "helm/roles/"+r+".yaml", "helm/providers/"+p+".yaml")
			valueSets = append(valueSets, valueSet{
				Suffix: "-role-" + r + "-provider-" + p,
				Values: comboValues,
			})
		}
	}

	unprofiled := make([]valueSet, len(valueSets))
	copy(unprofiled, valueSets)

	ppMap := make(map[string]bool)
	for _, pp := range profileProviders {
		ppMap[pp] = true
	}

	for _, baseSet := range unprofiled {
		for _, prof := range profiles {
			profValues := append(slicesClone(baseSet.Values), "helm/profiles/"+prof+".yaml")
			for pp := range ppMap {
				if strings.Contains(baseSet.Suffix, "-provider-"+pp) {
					profValues = append(profValues, "helm/profile_providers/"+pp+".yaml")
				}
			}
			valueSets = append(valueSets, valueSet{
				Suffix: baseSet.Suffix + "-profile-" + prof,
				Values: profValues,
			})
		}
	}

	clientImage := filepath.Join(helmDir, "client-image.yaml")
	if _, err := os.Stat(clientImage); err == nil {
		for i := range valueSets {
			valueSets[i].Values = append(valueSets[i].Values, "helm/client-image.yaml")
		}
	}

	return valueSets
}

func listYamlStems(dir string) []string {
	entries, err := os.ReadDir(dir)
	if err != nil {
		return nil
	}
	var stems []string
	for _, e := range entries {
		if e.IsDir() {
			continue
		}
		name := e.Name()
		if strings.HasSuffix(name, ".yaml") {
			stems = append(stems, strings.TrimSuffix(name, ".yaml"))
		}
	}
	sort.Strings(stems)
	return stems
}

func slicesClone(s []string) []string {
	c := make([]string, len(s))
	copy(c, s)
	return c
}
