// Defines snapshot manifest schema and metadata types for workspace backups.

package model

import "time"

// SnapshotManifest represents Schema 3 of the workspace snapshot manifest.
type SnapshotManifest struct {
	Schema           int    `json:"schema"`
	Selector         string `json:"selector"`
	Display          string `json:"display"`
	LineageToken     string `json:"lineageToken"`
	ParentLineage    string `json:"parentLineage"`
	ParentSnapshot   string `json:"parentSnapshot"`
	IsRoot           bool   `json:"isRoot"`
	SourceHostDigest string `json:"sourceHostDigest"`
	ScopeDigest      string `json:"scopeDigest"`
	Timestamp        int64  `json:"timestamp"`
	SizeBytes        int64  `json:"sizeBytes"`
	FilesCount       int64  `json:"filesCount"`
	KopiaSnapshotID  string `json:"kopiaSnapshotId"`
	Cell             string `json:"cell,omitempty"`
	Team             string `json:"team,omitempty"`
}

// Time converts the Unix timestamp to time.Time, supporting nanoseconds, milliseconds, or seconds.
func (m *SnapshotManifest) Time() time.Time {
	if m.Timestamp > 1e16 {
		return time.Unix(0, m.Timestamp)
	}
	if m.Timestamp > 1e11 {
		return time.UnixMilli(m.Timestamp)
	}
	return time.Unix(m.Timestamp, 0)
}
