// Find the components that OpenTofu topologies call, whose validation the topology validate tests already cover.

package opentofu

import (
	"io/fs"
	"path/filepath"
	"strings"
)

const topologiesRel = "src/infra/terraform/topologies"

// topologyCalledModules returns the labels of every module that a topology
// module calls directly. `tofu validate` on a topology validates each module
// it calls, so these modules need no validate test of their own.
func topologyCalledModules(repoRoot string) (map[string]bool, error) {
	filesByDir := make(map[string][]string)
	err := filepath.WalkDir(filepath.Join(repoRoot, topologiesRel), func(path string, entry fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if !entry.IsDir() && strings.HasSuffix(path, ".tf") {
			dir := filepath.Dir(path)
			filesByDir[dir] = append(filesByDir[dir], entry.Name())
		}
		return nil
	})
	if err != nil {
		return nil, err
	}

	called := make(map[string]bool)
	for dir, files := range filesByDir {
		deps, err := parseModuleDependencies(dir, repoRoot, files)
		if err != nil {
			return nil, err
		}
		for _, dep := range deps {
			called[dep] = true
		}
	}
	return called, nil
}
