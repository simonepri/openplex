// Parse OpenTofu configuration files to extract providers, child module calls, and variable dependencies.

package opentofu

import (
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
)

var (
	providerEntryRe = regexp.MustCompile(`(?m)^\s*([a-zA-Z0-9_-]+)\s*=\s*\{`)
	moduleSourceRe  = regexp.MustCompile(`source\s*=\s*"(\.\.[^"]+)"`)
)

func parseProviders(versionsPath string) ([]string, error) {
	content, err := os.ReadFile(versionsPath)
	if err != nil {
		if os.IsNotExist(err) {
			return nil, nil
		}
		return nil, err
	}

	idx := strings.Index(string(content), "required_providers")
	if idx == -1 {
		return nil, nil
	}

	openIdx := strings.Index(string(content[idx:]), "{")
	if openIdx == -1 {
		return nil, nil
	}
	start := idx + openIdx + 1

	depth := 1
	end := start
	str := string(content)
	for end < len(str) && depth > 0 {
		if str[end] == '{' {
			depth++
		} else if str[end] == '}' {
			depth--
		}
		end++
	}

	body := content[start : end-1]
	matches := providerEntryRe.FindAllSubmatch(body, -1)
	if len(matches) == 0 {
		return nil, nil
	}

	seen := make(map[string]struct{})
	var providers []string
	for _, m := range matches {
		name := string(m[1])
		if _, ok := seen[name]; !ok {
			seen[name] = struct{}{}
			providers = append(providers, name)
		}
	}
	sort.Strings(providers)
	return providers, nil
}

func parseModuleDependencies(dir string, repoRoot string, files []string) ([]string, error) {
	seen := make(map[string]struct{})
	var deps []string

	for _, file := range files {
		if !strings.HasSuffix(file, ".tf") && !strings.HasSuffix(file, ".tftest.hcl") {
			continue
		}
		filePath := filepath.Join(dir, file)
		content, err := os.ReadFile(filePath)
		if err != nil {
			return nil, err
		}

		matches := moduleSourceRe.FindAllSubmatch(content, -1)
		for _, m := range matches {
			source := string(m[1])
			targetPath := filepath.Clean(filepath.Join(dir, source))

			// Strip testdata components if present
			parts := strings.Split(targetPath, string(filepath.Separator))
			for i, part := range parts {
				if part == "testdata" {
					targetPath = strings.Join(parts[:i], string(filepath.Separator))
					break
				}
			}

			rel, err := filepath.Rel(repoRoot, targetPath)
			if err != nil {
				continue
			}
			label := "//" + filepath.ToSlash(rel)
			if _, ok := seen[label]; !ok {
				seen[label] = struct{}{}
				deps = append(deps, label)
			}
		}
	}
	sort.Strings(deps)
	return deps, nil
}
