// Configures and runs the HTTP server and routing middleware for the snapshot portal.

package server

import (
	"context"
	"errors"
	"fmt"
	"html/template"
	"io/fs"
	"net/http"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	"go.invalid/internal/tools/coder_snapshot_portal/pkg/auth"
	"go.invalid/internal/tools/coder_snapshot_portal/pkg/storage"
	"go.invalid/internal/tools/coder_snapshot_portal/web"
)

type contextKey string

const sessionContextKey contextKey = "coder_snapshot_session"

// Config encapsulates configuration for the Snapshot Portal HTTP server.
type Config struct {
	CoderURL          string
	CoderTemplateName string
	OAuthConfig       *auth.OAuthConfig
	SessionManager    *auth.SessionManager
	Store             storage.SnapshotStore
	DevMode           bool
	TemplatesFS       fs.FS
	StaticFS          fs.FS
}

// Server serves the Coder snapshot portal web interface, API, and OAuth handlers.
type Server struct {
	cfg       Config
	mux       *http.ServeMux
	templates *template.Template
}

// NewServer initializes a new Server with required routes, middleware, and templates.
func NewServer(cfg Config) (*Server, error) {
	if cfg.CoderURL == "" {
		return nil, errors.New("coder URL cannot be empty")
	}
	if cfg.SessionManager == nil {
		return nil, errors.New("session manager is required")
	}
	if cfg.Store == nil {
		return nil, errors.New("snapshot store is required")
	}
	if cfg.CoderTemplateName == "" {
		cfg.CoderTemplateName = "dev"
	}

	templatesFS := cfg.TemplatesFS
	if templatesFS == nil {
		templatesFS = web.Templates
	}

	staticFS := cfg.StaticFS
	if staticFS == nil {
		staticFS = web.Static
	}

	funcMap := template.FuncMap{
		"formatBytes":             formatBytes,
		"formatNumber":            formatNumber,
		"formatRelativeTime":      formatRelativeTime,
		"formatRelativeTimeShort": formatRelativeTimeShort,
		"formatExactTime":         formatExactTime,
		"formatSnapshotTitle":     formatSnapshotTitle,
		"shortSelector":           shortSelector,
		"add": func(a, b float64) float64 {
			return a + b
		},
		"sub": func(a, b float64) float64 {
			return a - b
		},
	}

	tmpl := template.New("base").Funcs(funcMap)
	err := fs.WalkDir(templatesFS, ".", func(p string, d fs.DirEntry, walkErr error) error {
		if walkErr != nil {
			return walkErr
		}
		if d.IsDir() || (!strings.HasSuffix(p, ".tmpl") && !strings.HasSuffix(p, ".html")) {
			return nil
		}
		data, readErr := fs.ReadFile(templatesFS, p)
		if readErr != nil {
			return fmt.Errorf("failed to read template %q: %w", p, readErr)
		}
		name := filepath.Base(p)
		if _, parseErr := tmpl.New(name).Parse(string(data)); parseErr != nil {
			return fmt.Errorf("failed to parse template %q: %w", name, parseErr)
		}
		return nil
	})
	if err != nil {
		return nil, fmt.Errorf("failed to load web templates: %w", err)
	}

	s := &Server{
		cfg:       cfg,
		mux:       http.NewServeMux(),
		templates: tmpl,
	}

	s.routes(staticFS)
	return s, nil
}

func (s *Server) routes(staticFS fs.FS) {
	// Unauthenticated health endpoints
	s.mux.HandleFunc("GET /healthz", s.handleHealthz)
	s.mux.HandleFunc("GET /livez", s.handleLivez)

	// OAuth endpoints
	s.mux.HandleFunc("GET /oauth/login", s.handleOAuthLogin)
	s.mux.HandleFunc("GET /oauth/callback", s.handleOAuthCallback)
	s.mux.HandleFunc("GET /logout", s.handleLogout)

	// Static assets handler with cache revalidation
	subStatic, err := fs.Sub(staticFS, "static")
	if err != nil {
		subStatic = staticFS
	}
	fileServer := http.StripPrefix("/static/", http.FileServer(http.FS(subStatic)))
	s.mux.HandleFunc("GET /static/", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Cache-Control", "no-cache, must-revalidate")
		fileServer.ServeHTTP(w, r)
	})

	// Protected routes requiring valid session
	s.mux.HandleFunc("GET /", s.requireAuth(s.handleDashboard))
	s.mux.HandleFunc("GET /restore", s.requireAuth(s.handleRestore))
	s.mux.HandleFunc("GET /api/v1/snapshots", s.requireAuth(s.handleAPISnapshots))
}

// ServeHTTP dispatches requests to the server mux.
func (s *Server) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	s.mux.ServeHTTP(w, r)
}

// requireAuth middleware validates the signed session cookie before delegating to the handler.
func (s *Server) requireAuth(next http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		sess, err := s.cfg.SessionManager.GetSession(r)
		if err != nil || sess == nil || sess.IsExpired() {
			http.Redirect(w, r, "/oauth/login", http.StatusFound)
			return
		}

		ctx := context.WithValue(r.Context(), sessionContextKey, sess)
		next(w, r.WithContext(ctx))
	}
}

// SessionFromContext extracts the authenticated session from context.
func SessionFromContext(ctx context.Context) *auth.Session {
	if sess, ok := ctx.Value(sessionContextKey).(*auth.Session); ok {
		return sess
	}
	return nil
}

func formatBytes(b int64) string {
	if b <= 0 {
		return "0 B"
	}
	units := []string{"B", "KiB", "MiB", "GiB", "TiB", "PiB"}
	v := float64(b)
	i := 0
	for v >= 1024 && i < len(units)-1 {
		v /= 1024
		i++
	}
	if i == 0 || v >= 10 {
		return fmt.Sprintf("%.0f %s", v, units[i])
	}
	return fmt.Sprintf("%.1f %s", v, units[i])
}

func formatNumber(n int64) string {
	in := strconv.FormatInt(n, 10)
	numOfDigits := len(in)
	if numOfDigits <= 3 {
		return in
	}
	var b strings.Builder
	rem := numOfDigits % 3
	if rem > 0 {
		b.WriteString(in[:rem])
	}
	for i := rem; i < numOfDigits; i += 3 {
		if b.Len() > 0 {
			b.WriteByte(',')
		}
		b.WriteString(in[i : i+3])
	}
	return b.String()
}

func formatRelativeTime(t time.Time) string {
	if t.IsZero() {
		return "unknown"
	}
	d := time.Since(t)
	if d < 0 {
		d = 0
	}
	switch {
	case d < time.Minute:
		return "just now"
	case d < time.Hour:
		m := int(d.Minutes())
		if m == 1 {
			return "1 minute ago"
		}
		return fmt.Sprintf("%d minutes ago", m)
	case d < 24*time.Hour:
		h := int(d.Hours())
		if h == 1 {
			return "1 hour ago"
		}
		return fmt.Sprintf("%d hours ago", h)
	case d < 7*24*time.Hour:
		days := int(d.Hours() / 24)
		if days == 1 {
			return "yesterday"
		}
		return fmt.Sprintf("%d days ago", days)
	case d < 30*24*time.Hour:
		weeks := int(d.Hours() / (24 * 7))
		if weeks == 1 {
			return "1 week ago"
		}
		return fmt.Sprintf("%d weeks ago", weeks)
	default:
		months := int(d.Hours() / (24 * 30))
		if months == 1 {
			return "1 month ago"
		}
		return fmt.Sprintf("%d months ago", months)
	}
}

func formatRelativeTimeShort(t time.Time) string {
	if t.IsZero() {
		return ""
	}
	d := time.Since(t)
	if d < 0 {
		d = 0
	}
	switch {
	case d < time.Minute:
		return "now"
	case d < time.Hour:
		return fmt.Sprintf("%dm ago", int(d.Minutes()))
	case d < 24*time.Hour:
		return fmt.Sprintf("%dh ago", int(d.Hours()))
	case d < 7*24*time.Hour:
		return fmt.Sprintf("%dd ago", int(d.Hours()/24))
	case d < 30*24*time.Hour:
		return fmt.Sprintf("%dw ago", int(d.Hours()/(24*7)))
	default:
		return fmt.Sprintf("%dmo ago", int(d.Hours()/(24*30)))
	}
}

func formatExactTime(t time.Time) string {
	if t.IsZero() {
		return "unknown"
	}
	return t.UTC().Format("2006-01-02 15:04:05 UTC")
}

func shortSelector(s string) string {
	if len(s) > 16 {
		return s[:12] + "…"
	}
	return s
}

func formatSnapshotTitle(display, selector string) string {
	if display == "" {
		return selector
	}
	if idx := strings.Index(display, " | "); idx != -1 {
		prefix := strings.TrimSpace(display[:idx])
		suffix := strings.TrimSpace(display[idx+3:])
		if len(suffix) >= 10 && strings.Contains(suffix, "T") {
			return prefix
		}
	}
	return display
}
