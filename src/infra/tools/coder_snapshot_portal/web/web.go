// Embeds HTML templates and static assets for the Coder snapshot portal web interface.

package web

import "embed"

// Templates holds embedded HTML templates.
//
//go:embed templates/*
var Templates embed.FS

// Static holds embedded static web assets like stylesheets and icons.
//
//go:embed static/*
var Static embed.FS
