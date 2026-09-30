// Tests lineage tree construction, parent-child snapshot relationships, and metrics rollup.

package model

import (
	"strings"
	"testing"
	"time"
)

func TestBuildLineageTreeLinearChain(t *testing.T) {
	snapshots := []*SnapshotManifest{
		{
			Selector:     "snap-1",
			Display:      "Initial commit",
			LineageToken: "lin-main",
			IsRoot:       true,
			Timestamp:    1000,
		},
		{
			Selector:       "snap-2",
			Display:        "Add feature",
			LineageToken:   "lin-main",
			ParentSnapshot: "snap-1",
			ParentLineage:  "lin-main",
			Timestamp:      2000,
		},
		{
			Selector:       "snap-3",
			Display:        "Fix bug",
			LineageToken:   "lin-main",
			ParentSnapshot: "snap-2",
			ParentLineage:  "lin-main",
			Timestamp:      3000,
		},
	}

	tree := BuildLineageTree(snapshots)

	if len(tree.Roots) != 1 {
		t.Fatalf("expected 1 root, got %d", len(tree.Roots))
	}
	root := tree.Roots[0]
	if root.Snapshot.Selector != "snap-1" {
		t.Errorf("expected root snap-1, got %s", root.Snapshot.Selector)
	}
	if root.Depth != 0 {
		t.Errorf("expected root depth 0, got %d", root.Depth)
	}
	if len(root.Children) != 1 {
		t.Fatalf("expected root to have 1 child, got %d", len(root.Children))
	}

	child := root.Children[0]
	if child.Snapshot.Selector != "snap-2" {
		t.Errorf("expected child snap-2, got %s", child.Snapshot.Selector)
	}
	if child.Depth != 1 {
		t.Errorf("expected child depth 1, got %d", child.Depth)
	}
	if len(child.Children) != 1 {
		t.Fatalf("expected child to have 1 child, got %d", len(child.Children))
	}

	grandchild := child.Children[0]
	if grandchild.Snapshot.Selector != "snap-3" {
		t.Errorf("expected grandchild snap-3, got %s", grandchild.Snapshot.Selector)
	}
	if grandchild.Depth != 2 {
		t.Errorf("expected grandchild depth 2, got %d", grandchild.Depth)
	}
}

func TestBuildLineageTreePeriodicSnapshotsChaining(t *testing.T) {
	// Replicates workspaces taking periodic sync snapshots where ParentSnapshot is unset
	// and isRoot was recorded as true on each snapshot manifest.
	snapshots := []*SnapshotManifest{
		{Selector: "snap-1", Timestamp: 1000, LineageToken: "lin-work", IsRoot: true},
		{Selector: "snap-2", Timestamp: 2000, LineageToken: "lin-work", IsRoot: true, ParentSnapshot: ""},
		{Selector: "snap-3", Timestamp: 3000, LineageToken: "lin-work", IsRoot: true, ParentSnapshot: ""},
		{Selector: "snap-4", Timestamp: 4000, LineageToken: "lin-work", IsRoot: true, ParentSnapshot: ""},
	}

	tree := BuildLineageTree(snapshots)

	if len(tree.Roots) != 1 {
		t.Fatalf("expected exactly 1 root for sequential lineage snapshots, got %d", len(tree.Roots))
	}
	root := tree.Roots[0]
	if root.Snapshot.Selector != "snap-1" {
		t.Errorf("expected root snap-1, got %s", root.Snapshot.Selector)
	}
	if !root.IsRoot() {
		t.Errorf("expected root.IsRoot() to be true")
	}

	// Verify sequential chain snap-1 -> snap-2 -> snap-3 -> snap-4
	curr := root
	expectedSelectors := []string{"snap-1", "snap-2", "snap-3", "snap-4"}
	for i, expected := range expectedSelectors {
		if curr.Snapshot.Selector != expected {
			t.Errorf("expected node %d to be %s, got %s", i, expected, curr.Snapshot.Selector)
		}
		if i > 0 && curr.IsRoot() {
			t.Errorf("node %s has a parent and must not be root", curr.Snapshot.Selector)
		}
		if curr.Depth != i {
			t.Errorf("expected node %s to have depth %d, got %d", curr.Snapshot.Selector, i, curr.Depth)
		}
		if i < len(expectedSelectors)-1 {
			if len(curr.Children) != 1 {
				t.Fatalf("expected node %s to have 1 child, got %d", curr.Snapshot.Selector, len(curr.Children))
			}
			curr = curr.Children[0]
		}
	}

	opts := DefaultLayoutOptions()
	tree.ComputeLayout(opts)
	edges := tree.Edges(opts)
	if len(edges) != 3 {
		t.Fatalf("expected 3 connecting edges, got %d", len(edges))
	}
}

func TestBuildLineageTreeBranchingForks(t *testing.T) {
	snapshots := []*SnapshotManifest{
		{
			Selector:     "root",
			LineageToken: "lin-main",
			IsRoot:       true,
			Timestamp:    1000,
		},
		{
			Selector:       "child-a",
			LineageToken:   "lin-main",
			ParentSnapshot: "root",
			Timestamp:      2000,
		},
		{
			Selector:       "child-b",
			LineageToken:   "lin-main",
			ParentSnapshot: "root",
			Timestamp:      3000,
		},
	}

	tree := BuildLineageTree(snapshots)

	if len(tree.Roots) != 1 {
		t.Fatalf("expected 1 root, got %d", len(tree.Roots))
	}
	root := tree.Roots[0]
	if len(root.Children) != 2 {
		t.Fatalf("expected 2 children, got %d", len(root.Children))
	}
	if root.Children[0].Snapshot.Selector != "child-a" || root.Children[1].Snapshot.Selector != "child-b" {
		t.Errorf("unexpected children ordering: %s, %s", root.Children[0].Snapshot.Selector, root.Children[1].Snapshot.Selector)
	}
}

func TestBuildLineageTreeMultipleLineagesAndBranchRoots(t *testing.T) {
	snapshots := []*SnapshotManifest{
		{
			Selector:     "base",
			LineageToken: "lin-main",
			IsRoot:       true,
			Timestamp:    1000,
		},
		{
			Selector:       "fork-node",
			LineageToken:   "lin-feature",
			ParentSnapshot: "base",
			ParentLineage:  "lin-main",
			Timestamp:      2000,
		},
		{
			Selector:     "independent-root",
			LineageToken: "lin-other",
			IsRoot:       true,
			Timestamp:    3000,
		},
	}

	tree := BuildLineageTree(snapshots)

	if len(tree.Roots) != 2 {
		t.Fatalf("expected 2 roots, got %d", len(tree.Roots))
	}

	forkNode := tree.FindBySelector("fork-node")
	if forkNode == nil {
		t.Fatal("expected fork-node to exist")
	}
	if !forkNode.IsBranchRoot {
		t.Errorf("expected fork-node to have IsBranchRoot = true")
	}

	featureLineage := tree.FilterByLineage("lin-feature")
	if len(featureLineage) != 1 || featureLineage[0].Snapshot.Selector != "fork-node" {
		t.Errorf("unexpected feature lineage filter results")
	}
}

func TestBuildLineageTreeOrphanHandling(t *testing.T) {
	snapshots := []*SnapshotManifest{
		{
			Selector:       "orphan-1",
			ParentSnapshot: "missing-parent",
			ParentLineage:  "lin-base",
			LineageToken:   "lin-fork",
			IsRoot:         false,
			Timestamp:      1000,
		},
		{
			Selector:       "orphan-child",
			ParentSnapshot: "orphan-1",
			LineageToken:   "lin-fork",
			Timestamp:      2000,
		},
	}

	tree := BuildLineageTree(snapshots)

	if len(tree.Roots) != 1 {
		t.Fatalf("expected orphan to be treated as root, got %d roots", len(tree.Roots))
	}
	orphanRoot := tree.Roots[0]
	if orphanRoot.Snapshot.Selector != "orphan-1" {
		t.Errorf("expected orphan-1 as root, got %s", orphanRoot.Snapshot.Selector)
	}
	if orphanRoot.Depth != 0 {
		t.Errorf("expected orphan root depth 0, got %d", orphanRoot.Depth)
	}
	if !orphanRoot.IsBranchRoot {
		t.Errorf("expected orphan-1 with ParentLineage != LineageToken to be branch root")
	}
	if len(orphanRoot.Children) != 1 || orphanRoot.Children[0].Snapshot.Selector != "orphan-child" {
		t.Errorf("expected orphan-child under orphan-1")
	}
	if orphanRoot.Children[0].Depth != 1 {
		t.Errorf("expected orphan-child depth 1, got %d", orphanRoot.Children[0].Depth)
	}
}

func TestBuildLineageTreeCycleProtection(t *testing.T) {
	snapshots := []*SnapshotManifest{
		{
			Selector:       "node-a",
			ParentSnapshot: "node-b",
			Timestamp:      1000,
		},
		{
			Selector:       "node-b",
			ParentSnapshot: "node-a",
			Timestamp:      2000,
		},
	}

	tree := BuildLineageTree(snapshots)
	if len(tree.Roots) == 0 {
		t.Fatal("expected cycle to be broken and yield at least one root")
	}
}

func TestTopologicalSort(t *testing.T) {
	snapshots := []*SnapshotManifest{
		{Selector: "c", ParentSnapshot: "b", Timestamp: 3000},
		{Selector: "a", IsRoot: true, Timestamp: 1000},
		{Selector: "b", ParentSnapshot: "a", Timestamp: 2000},
	}

	tree := BuildLineageTree(snapshots)
	order := tree.TopologicalSort()

	if len(order) != 3 {
		t.Fatalf("expected 3 nodes, got %d", len(order))
	}
	if order[0].Snapshot.Selector != "a" || order[1].Snapshot.Selector != "b" || order[2].Snapshot.Selector != "c" {
		t.Errorf("expected order a, b, c; got %s, %s, %s",
			order[0].Snapshot.Selector, order[1].Snapshot.Selector, order[2].Snapshot.Selector)
	}
}

func TestTimelineGrouping(t *testing.T) {
	t1 := time.Date(2026, time.September, 15, 10, 0, 0, 0, time.UTC).Unix()
	t2 := time.Date(2026, time.September, 15, 14, 0, 0, 0, time.UTC).Unix()
	t3 := time.Date(2026, time.September, 16, 9, 0, 0, 0, time.UTC).Unix()

	snapshots := []*SnapshotManifest{
		{Selector: "s1", Timestamp: t1, IsRoot: true},
		{Selector: "s2", Timestamp: t2, ParentSnapshot: "s1"},
		{Selector: "s3", Timestamp: t3, ParentSnapshot: "s2"},
	}

	tree := BuildLineageTree(snapshots)

	days := tree.GroupByDay()
	if len(days) != 2 {
		t.Fatalf("expected 2 day groups, got %d", len(days))
	}
	if days[0].Key != "2026-09-16" {
		t.Errorf("expected first group 2026-09-16, got %s", days[0].Key)
	}
	if len(days[0].Nodes) != 1 || days[0].Count != 1 {
		t.Errorf("expected 1 node in first group, got len=%d count=%d", len(days[0].Nodes), days[0].Count)
	}
	if days[1].Key != "2026-09-15" {
		t.Errorf("expected second group 2026-09-15, got %s", days[1].Key)
	}
	if len(days[1].Nodes) != 2 || days[1].Count != 2 {
		t.Errorf("expected 2 nodes in second group, got len=%d count=%d", len(days[1].Nodes), days[1].Count)
	}

	weeks := tree.GroupByWeek()
	if len(weeks) != 1 {
		t.Fatalf("expected 1 week group, got %d", len(weeks))
	}
	if weeks[0].Count != 3 {
		t.Errorf("expected 3 nodes in week group, got %d", weeks[0].Count)
	}
	if !strings.Contains(weeks[0].Label, "Week 38") {
		t.Errorf("expected week label to contain Week 38, got %s", weeks[0].Label)
	}

	lineages := tree.GroupByLineage()
	if len(lineages) != 1 {
		t.Fatalf("expected 1 lineage group, got %d", len(lineages))
	}
	if lineages[0].Count != 3 {
		t.Errorf("expected 3 nodes in lineage group, got %d", lineages[0].Count)
	}
}

func TestSearchAndFilter(t *testing.T) {
	snapshots := []*SnapshotManifest{
		{
			Selector:        "alpha-prod-01",
			Display:         "Production release",
			LineageToken:    "lin-prod",
			Cell:            "cell-us",
			Team:            "infra",
			KopiaSnapshotID: "k-100",
			IsRoot:          true,
			Timestamp:       1000,
		},
		{
			Selector:        "beta-dev-02",
			Display:         "Dev test snapshot",
			LineageToken:    "lin-dev",
			Cell:            "cell-eu",
			Team:            "core",
			KopiaSnapshotID: "k-200",
			ParentSnapshot:  "alpha-prod-01",
			Timestamp:       2000,
		},
	}

	tree := BuildLineageTree(snapshots)

	if len(tree.Search("")) != 2 {
		t.Errorf("expected empty search to return all nodes")
	}

	prodMatches := tree.Search("PROD")
	if len(prodMatches) != 1 || prodMatches[0].Snapshot.Selector != "alpha-prod-01" {
		t.Errorf("unexpected matches for 'PROD'")
	}

	cellMatches := tree.Search("cell-eu")
	if len(cellMatches) != 1 || cellMatches[0].Snapshot.Selector != "beta-dev-02" {
		t.Errorf("unexpected matches for 'cell-eu'")
	}

	teamMatches := tree.Search("core")
	if len(teamMatches) != 1 || teamMatches[0].Snapshot.Selector != "beta-dev-02" {
		t.Errorf("unexpected matches for 'core'")
	}

	nonExistent := tree.FindBySelector("non-existent")
	if nonExistent != nil {
		t.Errorf("expected nil for non-existent selector")
	}
}

func TestComputeLayoutAndEdges(t *testing.T) {
	snapshots := []*SnapshotManifest{
		{Selector: "root", IsRoot: true, Timestamp: 1000, LineageToken: "lin-main"},
		{Selector: "child1", ParentSnapshot: "root", Timestamp: 2000, LineageToken: "lin-main"},
		{Selector: "child2", ParentSnapshot: "root", Timestamp: 3000, LineageToken: "lin-feature"},
	}

	tree := BuildLineageTree(snapshots)
	opts := DefaultLayoutOptions()
	tree.ComputeLayout(opts)

	root := tree.FindBySelector("root")
	child1 := tree.FindBySelector("child1")
	child2 := tree.FindBySelector("child2")

	if root.X != opts.MarginX || root.Y != opts.MarginY {
		t.Errorf("unexpected root coordinates: (%f, %f)", root.X, root.Y)
	}
	if child1.X <= root.X {
		t.Errorf("expected child1.X > root.X, got %f <= %f", child1.X, root.X)
	}
	if child2.X <= child1.X {
		t.Errorf("expected time-aligned child2.X > child1.X, got %f <= %f", child2.X, child1.X)
	}
	if child2.Y <= child1.Y {
		t.Errorf("expected child2.Y > child1.Y for branched child, got %f <= %f", child2.Y, child1.Y)
	}
	if root.Color == "" || child1.Color == "" || child2.Color == "" {
		t.Errorf("expected colors to be assigned to all nodes")
	}
	if child1.Color != root.Color {
		t.Errorf("expected same lineage child1 to share color with root, got %q != %q", child1.Color, root.Color)
	}
	if child2.Color == root.Color {
		t.Errorf("expected distinct lineage child2 to have different color from root, got %q", child2.Color)
	}

	edges := tree.Edges(opts)
	if len(edges) != 2 {
		t.Fatalf("expected 2 edges, got %d", len(edges))
	}
	for _, edge := range edges {
		if !strings.HasPrefix(edge.Path, "M ") {
			t.Errorf("expected valid SVG path starting with M, got %q", edge.Path)
		}
		if edge.Color == "" {
			t.Errorf("expected edge color to be set")
		}
	}

	// Test vertical layout
	optsVert := opts
	optsVert.Orientation = LayoutVertical
	tree.ComputeLayout(optsVert)
	vertEdges := tree.Edges(optsVert)
	if len(vertEdges) != 2 {
		t.Fatalf("expected 2 vertical edges, got %d", len(vertEdges))
	}
}

func TestComputeLayoutSequentialTimeline(t *testing.T) {
	snapshots := []*SnapshotManifest{
		{Selector: "snap-a1", Timestamp: 1000, LineageToken: "lin-1", IsRoot: true},
		{Selector: "snap-a2", Timestamp: 2000, LineageToken: "lin-1", ParentSnapshot: "snap-a1"},
		{Selector: "snap-b1", Timestamp: 3000, LineageToken: "lin-2", IsRoot: true},
	}
	tree := BuildLineageTree(snapshots)
	opts := DefaultLayoutOptions()
	tree.ComputeLayout(opts)

	nodeA1 := tree.FindBySelector("snap-a1")
	nodeA2 := tree.FindBySelector("snap-a2")
	nodeB1 := tree.FindBySelector("snap-b1")

	if nodeA2.X <= nodeA1.X {
		t.Errorf("expected nodeA2.X > nodeA1.X, got %f <= %f", nodeA2.X, nodeA1.X)
	}
	if nodeB1.X <= nodeA2.X {
		t.Errorf("expected disconnected root nodeB1.X to appear after nodeA2.X on timeline, got %f <= %f", nodeB1.X, nodeA2.X)
	}
	if nodeB1.Y <= nodeA1.Y {
		t.Errorf("expected distinct lineage root nodeB1 to have its own track/lane, got Y=%f want Y > %f", nodeB1.Y, nodeA1.Y)
	}
	if nodeB1.Color == nodeA1.Color {
		t.Errorf("expected distinct lineages to receive distinct colors, got %q == %q", nodeB1.Color, nodeA1.Color)
	}
}

