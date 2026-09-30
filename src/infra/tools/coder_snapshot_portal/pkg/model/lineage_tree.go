// Constructs and traverses hierarchical snapshot lineage trees with storage rollups.

package model

import (
	"fmt"
	"sort"
	"strings"
	"time"
)

// TreeNode represents a node in the lineage DAG.
type TreeNode struct {
	Snapshot     *SnapshotManifest `json:"snapshot"`
	Children     []*TreeNode       `json:"children"`
	Parent       *TreeNode         `json:"-"`
	Depth        int               `json:"depth"`
	IsBranchRoot bool              `json:"isBranchRoot"`
	X            float64           `json:"x"`
	Y            float64           `json:"y"`
	Col          int               `json:"col"`
	Lane         int               `json:"lane"`
	Color        string            `json:"color"`
	BorderColor  string            `json:"borderColor"`
	BgColor      string            `json:"bgColor"`
}

// IsRoot returns true if this node is a root of the DAG (has no parent).
func (n *TreeNode) IsRoot() bool {
	return n.Parent == nil
}

// LineageTree represents a collection of snapshots structured as a Directed Acyclic Graph.
type LineageTree struct {
	Roots     []*TreeNode            `json:"roots"`
	Nodes     map[string]*TreeNode   `json:"nodes"`
	Lineages  map[string][]*TreeNode `json:"lineages"`
	Snapshots []*SnapshotManifest    `json:"snapshots"`
}

// TimelineGroup represents a chronological or structural group of snapshot nodes.
type TimelineGroup struct {
	Key       string      `json:"key"`
	Label     string      `json:"label"`
	Date      time.Time   `json:"date"`
	Nodes     []*TreeNode `json:"nodes"`
	TotalSize int64       `json:"totalSize"`
	Count     int         `json:"count"`
}

// Edge represents a directed link between two nodes with an SVG path string.
type Edge struct {
	From  *TreeNode `json:"from"`
	To    *TreeNode `json:"to"`
	Path  string    `json:"path"`
	Color string    `json:"color"`
}

// LineageColorDef defines the palette styling for a lineage branch and its dots.
type LineageColorDef struct {
	Color       string `json:"color"`
	BorderColor string `json:"borderColor"`
	BgColor     string `json:"bgColor"`
}

// LineagePalette provides distinct, accessible dark-theme colors for lineage rails.
var LineagePalette = []LineageColorDef{
	{Color: "#a855f7", BorderColor: "#9333ea", BgColor: "rgba(168, 85, 247, 0.15)"}, // Purple
	{Color: "#06b6d4", BorderColor: "#0891b2", BgColor: "rgba(6, 182, 212, 0.15)"},  // Cyan
	{Color: "#f59e0b", BorderColor: "#d97706", BgColor: "rgba(245, 158, 11, 0.15)"}, // Amber
	{Color: "#ec4899", BorderColor: "#db2777", BgColor: "rgba(236, 72, 153, 0.15)"}, // Pink
	{Color: "#10b981", BorderColor: "#059669", BgColor: "rgba(16, 185, 129, 0.15)"}, // Emerald
	{Color: "#3b82f6", BorderColor: "#2563eb", BgColor: "rgba(59, 130, 246, 0.15)"}, // Blue
	{Color: "#f97316", BorderColor: "#ea580c", BgColor: "rgba(249, 115, 22, 0.15)"}, // Orange
	{Color: "#14b8a6", BorderColor: "#0d9488", BgColor: "rgba(20, 184, 166, 0.15)"}, // Teal
}

func getLineageColor(lineageToken string, lineageIndex map[string]int) LineageColorDef {
	if lineageToken == "" {
		lineageToken = "default"
	}
	idx, exists := lineageIndex[lineageToken]
	if !exists {
		idx = len(lineageIndex)
		lineageIndex[lineageToken] = idx
	}
	return LineagePalette[idx%len(LineagePalette)]
}

// LayoutOrientation specifies the visual progression direction for layout computation.
type LayoutOrientation string

const (
	LayoutHorizontal LayoutOrientation = "horizontal"
	LayoutVertical   LayoutOrientation = "vertical"
)

// LayoutOptions configures the SVG layout calculation.
type LayoutOptions struct {
	Orientation LayoutOrientation
	NodeWidth   float64
	NodeHeight  float64
	XSpacing    float64
	YSpacing    float64
	MarginX     float64
	MarginY     float64
}

// DefaultLayoutOptions returns standard dimensions for SVG git-rail lineage graph rendering.
func DefaultLayoutOptions() LayoutOptions {
	return LayoutOptions{
		Orientation: LayoutHorizontal,
		NodeWidth:   14,
		NodeHeight:  14,
		XSpacing:    52,
		YSpacing:    48,
		MarginX:     32,
		MarginY:     26,
	}
}

// BuildLineageTree constructs a DAG hierarchy from a flat list of snapshot manifests.
func BuildLineageTree(snapshots []*SnapshotManifest) *LineageTree {
	tree := &LineageTree{
		Roots:     make([]*TreeNode, 0),
		Nodes:     make(map[string]*TreeNode, len(snapshots)),
		Lineages:  make(map[string][]*TreeNode),
		Snapshots: snapshots,
	}

	for _, s := range snapshots {
		if s == nil || s.Selector == "" {
			continue
		}
		node := &TreeNode{
			Snapshot: s,
			Children: make([]*TreeNode, 0),
		}
		tree.Nodes[s.Selector] = node
		if s.LineageToken != "" {
			tree.Lineages[s.LineageToken] = append(tree.Lineages[s.LineageToken], node)
		}
	}

	// Step 1: Connect explicit parent-child links where ParentSnapshot is specified.
	for _, node := range tree.Nodes {
		s := node.Snapshot
		if s.ParentSnapshot == "" {
			continue
		}

		parent, exists := tree.Nodes[s.ParentSnapshot]
		if !exists {
			// Orphan or missing parent: retain as root and check branch root status
			if s.ParentLineage != "" && s.ParentLineage != s.LineageToken {
				node.IsBranchRoot = true
			}
			continue
		}

		if createsCycle(parent, node) {
			continue
		}

		parent.Children = append(parent.Children, node)
		node.Parent = parent

		if s.ParentLineage != "" && s.ParentLineage != s.LineageToken {
			node.IsBranchRoot = true
		} else if parent.Snapshot != nil && parent.Snapshot.LineageToken != s.LineageToken {
			node.IsBranchRoot = true
		}
	}

	// Step 2: For snapshots within the same lineage that lack an explicit parent,
	// infer sequential chronological parent-child continuity (e.g. periodic sync snapshots).
	for _, lineageNodes := range tree.Lineages {
		if len(lineageNodes) <= 1 {
			continue
		}
		sortNodes(lineageNodes)
		for i := 1; i < len(lineageNodes); i++ {
			curr := lineageNodes[i]
			if curr.Parent == nil && curr.Snapshot.ParentSnapshot == "" {
				prev := lineageNodes[i-1]
				if !createsCycle(prev, curr) {
					curr.Parent = prev
					prev.Children = append(prev.Children, curr)
				}
			}
		}
	}

	// Step 3: Collect roots and normalize IsRoot status.
	for _, node := range tree.Nodes {
		if node.Parent == nil {
			tree.Roots = append(tree.Roots, node)
			node.Snapshot.IsRoot = true
		} else {
			node.Snapshot.IsRoot = false
		}
	}

	sortNodes(tree.Roots)
	for _, node := range tree.Nodes {
		sortNodes(node.Children)
	}

	for _, root := range tree.Roots {
		assignDepth(root, 0)
	}

	return tree
}

func createsCycle(parent, candidate *TreeNode) bool {
	curr := parent
	for curr != nil {
		if curr == candidate {
			return true
		}
		curr = curr.Parent
	}
	return false
}

func assignDepth(node *TreeNode, depth int) {
	node.Depth = depth
	for _, child := range node.Children {
		assignDepth(child, depth+1)
	}
}

func sortNodes(nodes []*TreeNode) {
	sort.Slice(nodes, func(i, j int) bool {
		if nodes[i].Snapshot.Timestamp != nodes[j].Snapshot.Timestamp {
			return nodes[i].Snapshot.Timestamp < nodes[j].Snapshot.Timestamp
		}
		return nodes[i].Snapshot.Selector < nodes[j].Snapshot.Selector
	})
}

// TopologicalSort returns the nodes in DAG causal order (parents precede children).
func (t *LineageTree) TopologicalSort() []*TreeNode {
	inDegree := make(map[string]int, len(t.Nodes))
	for _, node := range t.Nodes {
		inDegree[node.Snapshot.Selector] = 0
	}
	for _, node := range t.Nodes {
		for _, child := range node.Children {
			inDegree[child.Snapshot.Selector]++
		}
	}

	queue := make([]*TreeNode, 0)
	for _, root := range t.Roots {
		if inDegree[root.Snapshot.Selector] == 0 {
			queue = append(queue, root)
		}
	}
	sortNodes(queue)

	result := make([]*TreeNode, 0, len(t.Nodes))
	visited := make(map[string]bool, len(t.Nodes))

	for len(queue) > 0 {
		curr := queue[0]
		queue = queue[1:]

		if visited[curr.Snapshot.Selector] {
			continue
		}
		visited[curr.Snapshot.Selector] = true
		result = append(result, curr)

		nextLevel := make([]*TreeNode, 0, len(curr.Children))
		for _, child := range curr.Children {
			inDegree[child.Snapshot.Selector]--
			if inDegree[child.Snapshot.Selector] <= 0 && !visited[child.Snapshot.Selector] {
				nextLevel = append(nextLevel, child)
			}
		}
		sortNodes(nextLevel)
		queue = append(queue, nextLevel...)
	}

	for _, node := range t.Nodes {
		if !visited[node.Snapshot.Selector] {
			result = append(result, node)
		}
	}

	return result
}

// GroupByDay partitions snapshots into day buckets ordered descending by date.
func (t *LineageTree) GroupByDay() []TimelineGroup {
	groupsMap := make(map[string]*TimelineGroup)
	keys := make([]string, 0)

	for _, node := range t.Nodes {
		tVal := node.Snapshot.Time().UTC()
		key := tVal.Format("2006-01-02")
		grp, ok := groupsMap[key]
		if !ok {
			dayStart := time.Date(tVal.Year(), tVal.Month(), tVal.Day(), 0, 0, 0, 0, time.UTC)
			grp = &TimelineGroup{
				Key:   key,
				Label: tVal.Format("January 02, 2006"),
				Date:  dayStart,
				Nodes: make([]*TreeNode, 0),
			}
			groupsMap[key] = grp
			keys = append(keys, key)
		}
		grp.Nodes = append(grp.Nodes, node)
		grp.TotalSize += node.Snapshot.SizeBytes
		grp.Count++
	}

	sort.Sort(sort.Reverse(sort.StringSlice(keys)))
	result := make([]TimelineGroup, 0, len(keys))
	for _, key := range keys {
		grp := groupsMap[key]
		sort.Slice(grp.Nodes, func(i, j int) bool {
			return grp.Nodes[i].Snapshot.Timestamp > grp.Nodes[j].Snapshot.Timestamp
		})
		result = append(result, *grp)
	}
	return result
}

// GroupByWeek partitions snapshots into ISO calendar week buckets ordered descending.
func (t *LineageTree) GroupByWeek() []TimelineGroup {
	groupsMap := make(map[string]*TimelineGroup)
	keys := make([]string, 0)

	for _, node := range t.Nodes {
		tVal := node.Snapshot.Time().UTC()
		year, week := tVal.ISOWeek()
		key := fmt.Sprintf("%04d-W%02d", year, week)
		grp, ok := groupsMap[key]
		if !ok {
			mon := isoWeekStart(year, week)
			sun := mon.AddDate(0, 0, 6)
			label := fmt.Sprintf("%s – %s (Week %d, %d)", mon.Format("Jan 02"), sun.Format("Jan 02, 2006"), week, year)
			grp = &TimelineGroup{
				Key:   key,
				Label: label,
				Date:  tVal,
				Nodes: make([]*TreeNode, 0),
			}
			groupsMap[key] = grp
			keys = append(keys, key)
		}
		grp.Nodes = append(grp.Nodes, node)
		grp.TotalSize += node.Snapshot.SizeBytes
		grp.Count++
	}

	sort.Sort(sort.Reverse(sort.StringSlice(keys)))
	result := make([]TimelineGroup, 0, len(keys))
	for _, key := range keys {
		grp := groupsMap[key]
		sort.Slice(grp.Nodes, func(i, j int) bool {
			return grp.Nodes[i].Snapshot.Timestamp > grp.Nodes[j].Snapshot.Timestamp
		})
		result = append(result, *grp)
	}
	return result
}

// GroupByLineage partitions snapshots by workspace lineage token ordered descending by newest snapshot.
func (t *LineageTree) GroupByLineage() []TimelineGroup {
	groupsMap := make(map[string]*TimelineGroup)
	keys := make([]string, 0)

	for _, node := range t.Nodes {
		token := node.Snapshot.LineageToken
		if token == "" {
			token = "default"
		}
		grp, ok := groupsMap[token]
		if !ok {
			label := fmt.Sprintf("Lineage: %s", token)
			if len(token) > 28 {
				label = fmt.Sprintf("Lineage: %s…", token[:24])
			}
			grp = &TimelineGroup{
				Key:   token,
				Label: label,
				Date:  node.Snapshot.Time().UTC(),
				Nodes: make([]*TreeNode, 0),
			}
			groupsMap[token] = grp
			keys = append(keys, token)
		}
		grp.Nodes = append(grp.Nodes, node)
		grp.TotalSize += node.Snapshot.SizeBytes
		grp.Count++
		if node.Snapshot.Timestamp > grp.Date.Unix() {
			grp.Date = node.Snapshot.Time().UTC()
		}
	}

	sort.Slice(keys, func(i, j int) bool {
		return groupsMap[keys[i]].Date.After(groupsMap[keys[j]].Date)
	})

	result := make([]TimelineGroup, 0, len(keys))
	for _, key := range keys {
		grp := groupsMap[key]
		sort.Slice(grp.Nodes, func(i, j int) bool {
			return grp.Nodes[i].Snapshot.Timestamp > grp.Nodes[j].Snapshot.Timestamp
		})
		result = append(result, *grp)
	}
	return result
}

func isoWeekStart(year, week int) time.Time {
	jan4 := time.Date(year, time.January, 4, 0, 0, 0, 0, time.UTC)
	dayOffset := int(jan4.Weekday())
	if dayOffset == 0 {
		dayOffset = 7
	}
	mondayWeek1 := jan4.AddDate(0, 0, -(dayOffset - 1))
	return mondayWeek1.AddDate(0, 0, (week-1)*7)
}

// Search matches snapshots by substring against selector, display, lineageToken, cell, or team.
func (t *LineageTree) Search(query string) []*TreeNode {
	q := strings.TrimSpace(strings.ToLower(query))
	if q == "" {
		return t.TopologicalSort()
	}

	matches := make([]*TreeNode, 0)
	for _, node := range t.TopologicalSort() {
		s := node.Snapshot
		if strings.Contains(strings.ToLower(s.Selector), q) ||
			strings.Contains(strings.ToLower(s.Display), q) ||
			strings.Contains(strings.ToLower(s.LineageToken), q) ||
			strings.Contains(strings.ToLower(s.Cell), q) ||
			strings.Contains(strings.ToLower(s.Team), q) ||
			strings.Contains(strings.ToLower(s.KopiaSnapshotID), q) {
			matches = append(matches, node)
		}
	}
	return matches
}

// FilterByLineage returns all nodes belonging to the specified lineage token.
func (t *LineageTree) FilterByLineage(lineageToken string) []*TreeNode {
	nodes, ok := t.Lineages[lineageToken]
	if !ok {
		return []*TreeNode{}
	}
	res := make([]*TreeNode, len(nodes))
	copy(res, nodes)
	sortNodes(res)
	return res
}

// FindBySelector returns the tree node matching selector or nil if not found.
func (t *LineageTree) FindBySelector(selector string) *TreeNode {
	return t.Nodes[selector]
}

// ComputeLayout calculates (X, Y) coordinates for each node in the tree along a chronological timeline.
// Snapshots are strictly time-aligned horizontally across all lineages and roots, and each branch/lineage
// is mapped to a designated rail lane with a distinct color from LineagePalette.
func (t *LineageTree) ComputeLayout(opts LayoutOptions) {
	if len(t.Nodes) == 0 {
		return
	}

	// Step 1: Assign lanes and colors across branches and lineages
	nextLane := 0
	lineageColors := make(map[string]int)

	sortNodes(t.Roots)

	for _, root := range t.Roots {
		assignLanesAndColors(root, &nextLane, lineageColors)
	}

	// Step 2: Assign time-aligned columns strictly in chronological order
	allNodes := make([]*TreeNode, 0, len(t.Nodes))
	for _, n := range t.Nodes {
		allNodes = append(allNodes, n)
	}

	sort.Slice(allNodes, func(i, j int) bool {
		if allNodes[i].Snapshot.Timestamp != allNodes[j].Snapshot.Timestamp {
			return allNodes[i].Snapshot.Timestamp < allNodes[j].Snapshot.Timestamp
		}
		return allNodes[i].Snapshot.Selector < allNodes[j].Snapshot.Selector
	})

	colMap := make(map[string]int, len(allNodes))
	currentCol := 0
	for _, node := range allNodes {
		col := currentCol
		if node.Parent != nil {
			if parentCol, ok := colMap[node.Parent.Snapshot.Selector]; ok {
				if col <= parentCol {
					col = parentCol + 1
				}
			}
		}
		colMap[node.Snapshot.Selector] = col
		node.Col = col
		currentCol = col + 1
	}

	// Step 3: Compute final (X, Y) coordinates
	for _, node := range allNodes {
		x := opts.MarginX + float64(node.Col)*opts.XSpacing
		y := opts.MarginY + float64(node.Lane)*opts.YSpacing
		if opts.Orientation == LayoutVertical {
			node.X = y
			node.Y = x
		} else {
			node.X = x
			node.Y = y
		}
	}
}

func assignLanesAndColors(node *TreeNode, nextLane *int, lineageColors map[string]int) {
	if node.Parent == nil {
		node.Lane = *nextLane
		*nextLane++
	}

	token := node.Snapshot.LineageToken
	if token == "" {
		token = "default"
	}
	colorDef := getLineageColor(token, lineageColors)
	node.Color = colorDef.Color
	node.BorderColor = colorDef.BorderColor
	node.BgColor = colorDef.BgColor

	sortNodes(node.Children)

	for i, child := range node.Children {
		if i == 0 && child.Snapshot.LineageToken == node.Snapshot.LineageToken {
			child.Lane = node.Lane
		} else {
			child.Lane = *nextLane
			*nextLane++
		}
		assignLanesAndColors(child, nextLane, lineageColors)
	}
}

// Edges returns directed links between all parents and children with SVG path strings.
func (t *LineageTree) Edges(opts LayoutOptions) []Edge {
	edges := make([]Edge, 0)
	for _, node := range t.Nodes {
		for _, child := range node.Children {
			var path string
			if opts.Orientation == LayoutVertical {
				if node.X == child.X {
					path = fmt.Sprintf("M %.1f %.1f L %.1f %.1f", node.X, node.Y, child.X, child.Y)
				} else {
					dy := (child.Y - node.Y) / 2
					path = fmt.Sprintf("M %.1f %.1f C %.1f %.1f, %.1f %.1f, %.1f %.1f",
						node.X, node.Y,
						node.X, node.Y+dy,
						child.X, child.Y-dy,
						child.X, child.Y)
				}
			} else {
				if node.Y == child.Y {
					path = fmt.Sprintf("M %.1f %.1f L %.1f %.1f", node.X, node.Y, child.X, child.Y)
				} else {
					dx := (child.X - node.X) / 2
					path = fmt.Sprintf("M %.1f %.1f C %.1f %.1f, %.1f %.1f, %.1f %.1f",
						node.X, node.Y,
						node.X+dx, node.Y,
						child.X-dx, child.Y,
						child.X, child.Y)
				}
			}
			edges = append(edges, Edge{
				From:  node,
				To:    child,
				Path:  path,
				Color: child.Color,
			})
		}
	}
	return edges
}

// CubicBezierPath generates a smooth SVG cubic Bézier curve between two points.
func CubicBezierPath(x1, y1, x2, y2 float64, orientation LayoutOrientation) string {
	if orientation == LayoutVertical {
		dy := (y2 - y1) / 2
		return fmt.Sprintf("M %.1f %.1f C %.1f %.1f, %.1f %.1f, %.1f %.1f", x1, y1, x1, y1+dy, x2, y2-dy, x2, y2)
	}
	dx := (x2 - x1) / 2
	return fmt.Sprintf("M %.1f %.1f C %.1f %.1f, %.1f %.1f, %.1f %.1f", x1, y1, x1+dx, y1, x2-dx, y2, x2, y2)
}

// OrthogonalPath generates a stepped orthogonal path between two points.
func OrthogonalPath(x1, y1, x2, y2 float64, orientation LayoutOrientation) string {
	if orientation == LayoutVertical {
		midY := (y1 + y2) / 2
		return fmt.Sprintf("M %.1f %.1f L %.1f %.1f L %.1f %.1f L %.1f %.1f", x1, y1, x1, midY, x2, midY, x2, y2)
	}
	midX := (x1 + x2) / 2
	return fmt.Sprintf("M %.1f %.1f L %.1f %.1f L %.1f %.1f L %.1f %.1f", x1, y1, midX, y1, midX, y2, x2, y2)
}
