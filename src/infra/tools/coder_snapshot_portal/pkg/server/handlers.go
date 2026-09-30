// Handles HTTP requests for snapshot browsing, catalog views, and auth endpoints.

package server

import (
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"net/http"
	"net/url"
	"sort"
	"strings"
	"time"

	"go.invalid/internal/tools/coder_snapshot_portal/pkg/auth"
	"go.invalid/internal/tools/coder_snapshot_portal/pkg/model"
)

// CatalogViewData encapsulates the data required by catalog.html.tmpl.
type CatalogViewData struct {
	User                   *auth.Session
	CoderURL               string
	CoderTemplateName      string
	GroupMode              string
	Groups                 []model.TimelineGroup
	AllLineages            []string
	Nodes                  []*model.TreeNode
	Edges                  []model.Edge
	SVGWidth               float64
	SVGHeight              float64
	NodeWidth              float64
	NodeHeight             float64
	SnapshotCount          int
	TotalSizeBytes         int64
	TotalSizeFormatted     string
	TotalPhysicalBytes     int64
	TotalPhysicalFormatted string
	PhysicalPercent        string
	DedupSavingsPercent    string
	DedupRatio             string
	LatestSelector         string
	Owner                  string
	Workspace              string
	HasWorkspaceContext    bool
	WorkspaceParametersURL string
	SelectedLineage        string
	ActiveSelector         string
}

func (s *Server) handleHealthz(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "text/plain; charset=utf-8")
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write([]byte("ok\n"))
}

func (s *Server) handleLivez(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "text/plain; charset=utf-8")
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write([]byte("ok\n"))
}

func (s *Server) handleOAuthLogin(w http.ResponseWriter, r *http.Request) {
	stateBytes := make([]byte, 16)
	if _, err := rand.Read(stateBytes); err != nil {
		http.Error(w, "failed to generate oauth state", http.StatusInternalServerError)
		return
	}
	state := hex.EncodeToString(stateBytes)

	codeVerifier, codeChallenge, err := auth.GeneratePKCE()
	if err != nil {
		http.Error(w, "failed to generate pkce challenge", http.StatusInternalServerError)
		return
	}

	// Set short-lived state cookie for CSRF defense
	http.SetCookie(w, &http.Cookie{
		Name:     "coder_oauth_state",
		Value:    state,
		Path:     "/",
		HttpOnly: true,
		Secure:   s.cfg.SessionManager.Secure,
		SameSite: http.SameSiteLaxMode,
		Expires:  time.Now().Add(10 * time.Minute),
		MaxAge:   600,
	})

	// Set short-lived verifier cookie for PKCE exchange
	http.SetCookie(w, &http.Cookie{
		Name:     "coder_oauth_verifier",
		Value:    codeVerifier,
		Path:     "/",
		HttpOnly: true,
		Secure:   s.cfg.SessionManager.Secure,
		SameSite: http.SameSiteLaxMode,
		Expires:  time.Now().Add(10 * time.Minute),
		MaxAge:   600,
	})

	if s.cfg.OAuthConfig == nil {
		if s.cfg.DevMode {
			// In dev mode without OAuth configured, automatically log in as dev user
			devSession := &auth.Session{
				UserID:    "dev-user",
				Username:  "dev-user",
				Email:     "dev@local.invalid",
				ExpiresAt: time.Now().Add(s.cfg.SessionManager.TTL),
			}
			cookie, err := s.cfg.SessionManager.CreateCookie(devSession)
			if err != nil {
				http.Error(w, "failed to create dev session", http.StatusInternalServerError)
				return
			}
			http.SetCookie(w, cookie)
			http.Redirect(w, r, "/", http.StatusFound)
			return
		}
		http.Error(w, "OAuth configuration is missing", http.StatusInternalServerError)
		return
	}

	authURL := s.cfg.OAuthConfig.AuthURL(state, codeChallenge)
	http.Redirect(w, r, authURL, http.StatusFound)
}

func (s *Server) handleOAuthCallback(w http.ResponseWriter, r *http.Request) {
	if errParam := r.URL.Query().Get("error"); errParam != "" {
		desc := r.URL.Query().Get("error_description")
		http.Error(w, fmt.Sprintf("OAuth error: %s (%s)", errParam, desc), http.StatusBadRequest)
		return
	}

	state := r.URL.Query().Get("state")
	stateCookie, err := r.Cookie("coder_oauth_state")
	if err != nil || state == "" || stateCookie.Value == "" || stateCookie.Value != state {
		http.Error(w, "invalid oauth state parameter", http.StatusBadRequest)
		return
	}
	verifierCookie, err := r.Cookie("coder_oauth_verifier")
	if err != nil || verifierCookie.Value == "" {
		http.Error(w, "missing oauth code verifier", http.StatusBadRequest)
		return
	}

	// Clear state cookie
	http.SetCookie(w, &http.Cookie{
		Name:     "coder_oauth_state",
		Value:    "",
		Path:     "/",
		HttpOnly: true,
		Secure:   s.cfg.SessionManager.Secure,
		SameSite: http.SameSiteLaxMode,
		Expires:  time.Unix(0, 0),
		MaxAge:   -1,
	})

	// Clear PKCE verifier cookie
	http.SetCookie(w, &http.Cookie{
		Name:     "coder_oauth_verifier",
		Value:    "",
		Path:     "/",
		HttpOnly: true,
		Secure:   s.cfg.SessionManager.Secure,
		SameSite: http.SameSiteLaxMode,
		Expires:  time.Unix(0, 0),
		MaxAge:   -1,
	})

	code := r.URL.Query().Get("code")
	if code == "" {
		http.Error(w, "missing code parameter", http.StatusBadRequest)
		return
	}

	if s.cfg.OAuthConfig == nil {
		http.Error(w, "OAuth configuration is missing", http.StatusInternalServerError)
		return
	}

	token, err := s.cfg.OAuthConfig.ExchangeCode(r.Context(), code, verifierCookie.Value)
	if err != nil {
		http.Error(w, fmt.Sprintf("failed to exchange code: %v", err), http.StatusInternalServerError)
		return
	}

	user, err := s.cfg.OAuthConfig.GetAuthenticatedUser(r.Context(), token)
	if err != nil {
		http.Error(w, fmt.Sprintf("failed to get user profile: %v", err), http.StatusInternalServerError)
		return
	}

	sess := &auth.Session{
		UserID:    user.ID,
		Username:  user.Username,
		Email:     user.Email,
		ExpiresAt: time.Now().Add(s.cfg.SessionManager.TTL),
	}

	cookie, err := s.cfg.SessionManager.CreateCookie(sess)
	if err != nil {
		http.Error(w, fmt.Sprintf("failed to issue session cookie: %v", err), http.StatusInternalServerError)
		return
	}

	http.SetCookie(w, cookie)
	http.Redirect(w, r, "/", http.StatusFound)
}

func (s *Server) handleLogout(w http.ResponseWriter, r *http.Request) {
	http.SetCookie(w, s.cfg.SessionManager.ClearCookie())
	http.Redirect(w, r, "/oauth/login", http.StatusFound)
}

func (s *Server) handleDashboard(w http.ResponseWriter, r *http.Request) {
	sess := SessionFromContext(r.Context())
	if sess == nil {
		http.Redirect(w, r, "/oauth/login", http.StatusFound)
		return
	}

	snapshots, err := s.cfg.Store.ListUserSnapshots(r.Context(), sess.UserID)
	if err != nil {
		http.Error(w, fmt.Sprintf("failed to load user snapshots: %v", err), http.StatusInternalServerError)
		return
	}

	tree := model.BuildLineageTree(snapshots)
	opts := model.DefaultLayoutOptions()
	tree.ComputeLayout(opts)
	edges := tree.Edges(opts)

	groupMode := r.URL.Query().Get("group")
	var groups []model.TimelineGroup
	switch groupMode {
	case "week":
		groups = tree.GroupByWeek()
	case "lineage":
		groups = tree.GroupByLineage()
	default:
		groupMode = "day"
		groups = tree.GroupByDay()
	}

	lineageSet := make(map[string]struct{})
	var latestSelector string
	var latestTimestamp int64

	for _, snap := range snapshots {
		if snap.LineageToken != "" {
			lineageSet[snap.LineageToken] = struct{}{}
		}
		if snap.Timestamp > latestTimestamp {
			latestTimestamp = snap.Timestamp
			latestSelector = snap.Selector
		}
	}

	allLineages := make([]string, 0, len(lineageSet))
	for l := range lineageSet {
		allLineages = append(allLineages, l)
	}
	sort.Strings(allLineages)

	storage := calculateStorageMetrics(snapshots)
	svgWidth, svgHeight := computeSVGDimensions(tree.Nodes, opts)

	owner := strings.TrimSpace(r.URL.Query().Get("owner"))
	workspace := strings.TrimSpace(r.URL.Query().Get("workspace"))
	hasWorkspaceContext := owner != "" && workspace != ""
	workspaceParametersURL := ""
	if hasWorkspaceContext {
		workspaceParametersURL = fmt.Sprintf("%s/@%s/%s/settings/parameters", strings.TrimRight(s.cfg.CoderURL, "/"), url.PathEscape(owner), url.PathEscape(workspace))
	}

	selectedLineage, activeSelector := resolveLineageContext(snapshots, workspace, strings.TrimSpace(r.URL.Query().Get("lineage")), latestSelector)

	data := &CatalogViewData{
		User:                   sess,
		CoderURL:               s.cfg.CoderURL,
		CoderTemplateName:      s.cfg.CoderTemplateName,
		GroupMode:              groupMode,
		Groups:                 groups,
		AllLineages:            allLineages,
		Nodes:                  tree.TopologicalSort(),
		Edges:                  edges,
		SVGWidth:               svgWidth,
		SVGHeight:              svgHeight,
		NodeWidth:              opts.NodeWidth,
		NodeHeight:             opts.NodeHeight,
		SnapshotCount:          len(snapshots),
		TotalSizeBytes:         storage.TotalLogicalBytes,
		TotalSizeFormatted:     formatBytes(storage.TotalLogicalBytes),
		TotalPhysicalBytes:     storage.TotalPhysicalBytes,
		TotalPhysicalFormatted: formatBytes(storage.TotalPhysicalBytes),
		PhysicalPercent:        storage.PhysicalPercent,
		DedupSavingsPercent:    storage.DedupSavingsPercent,
		DedupRatio:             storage.DedupRatio,
		LatestSelector:         latestSelector,
		ActiveSelector:         activeSelector,
		SelectedLineage:        selectedLineage,
		Owner:                  owner,
		Workspace:              workspace,
		HasWorkspaceContext:    hasWorkspaceContext,
		WorkspaceParametersURL: workspaceParametersURL,
	}

	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	if err := s.templates.ExecuteTemplate(w, "catalog.html.tmpl", data); err != nil {
		http.Error(w, fmt.Sprintf("failed to render catalog: %v", err), http.StatusInternalServerError)
		return
	}
}

type storageMetrics struct {
	TotalLogicalBytes   int64
	TotalPhysicalBytes  int64
	PhysicalPercent     string
	DedupSavingsPercent string
	DedupRatio          string
}

func resolveLineageContext(snapshots []*model.SnapshotManifest, workspace, requestedLineage, latestSelector string) (string, string) {
	selectedLineage := requestedLineage
	if selectedLineage == "" && workspace != "" {
		var latestWsTime int64
		for _, snap := range snapshots {
			if strings.HasPrefix(snap.Display, workspace+" |") || snap.Display == workspace {
				if snap.Timestamp > latestWsTime {
					latestWsTime = snap.Timestamp
					selectedLineage = snap.LineageToken
				}
			}
		}
	}

	activeSelector := latestSelector
	if selectedLineage != "" {
		var lineageLatestTime int64
		for _, snap := range snapshots {
			if snap.LineageToken == selectedLineage && snap.Timestamp > lineageLatestTime {
				lineageLatestTime = snap.Timestamp
				activeSelector = snap.Selector
			}
		}
	}
	return selectedLineage, activeSelector
}

func calculateStorageMetrics(snapshots []*model.SnapshotManifest) storageMetrics {
	var totalLogical int64
	lineageRootSizes := make(map[string]int64)
	lineageIncrementalCounts := make(map[string]int64)

	for _, snap := range snapshots {
		totalLogical += snap.SizeBytes
		lKey := snap.LineageToken
		if lKey == "" {
			lKey = "unassigned"
		}
		if _, exists := lineageRootSizes[lKey]; !exists {
			lineageRootSizes[lKey] = snap.SizeBytes
		} else {
			lineageIncrementalCounts[lKey]++
		}
	}

	var totalPhysical int64
	for lKey, rootSize := range lineageRootSizes {
		basePhysical := int64(float64(rootSize) * 0.45)
		if basePhysical < 50*1024*1024 && rootSize > 0 {
			basePhysical = 50 * 1024 * 1024
		}
		deltaPhysical := lineageIncrementalCounts[lKey] * 20 * 1024 * 1024
		totalPhysical += basePhysical + deltaPhysical
	}
	if totalPhysical > totalLogical {
		totalPhysical = totalLogical
	}

	physicalPercentStr := "100.0%"
	savingsPercent := "0.0%"
	ratioStr := "1.0x"
	if totalLogical > 0 && totalPhysical > 0 {
		pct := float64(totalPhysical) / float64(totalLogical) * 100.0
		if pct > 100.0 {
			pct = 100.0
		}
		savings := 100.0 - pct
		if savings < 0 {
			savings = 0
		}
		ratio := float64(totalLogical) / float64(totalPhysical)
		physicalPercentStr = fmt.Sprintf("%.1f%%", pct)
		savingsPercent = fmt.Sprintf("%.1f%%", savings)
		ratioStr = fmt.Sprintf("%.1fx", ratio)
	}

	return storageMetrics{
		TotalLogicalBytes:   totalLogical,
		TotalPhysicalBytes:  totalPhysical,
		PhysicalPercent:     physicalPercentStr,
		DedupSavingsPercent: savingsPercent,
		DedupRatio:          ratioStr,
	}
}

func computeSVGDimensions(nodes map[string]*model.TreeNode, opts model.LayoutOptions) (float64, float64) {
	var maxX, maxY float64
	for _, node := range nodes {
		if node.X > maxX {
			maxX = node.X
		}
		if node.Y > maxY {
			maxY = node.Y
		}
	}
	svgWidth := maxX + opts.MarginX + 36
	if svgWidth < 600 {
		svgWidth = 600
	}
	svgHeight := maxY + opts.MarginY + 36
	if svgHeight < 100 {
		svgHeight = 100
	}
	return svgWidth, svgHeight
}

func (s *Server) handleRestore(w http.ResponseWriter, r *http.Request) {
	selector := r.URL.Query().Get("selector")
	if selector == "" {
		http.Error(w, "missing selector parameter", http.StatusBadRequest)
		return
	}

	baseURL := strings.TrimRight(s.cfg.CoderURL, "/")
	restoreURL := fmt.Sprintf("%s/templates/%s/workspace?param.restore_selector=%s",
		baseURL,
		url.PathEscape(s.cfg.CoderTemplateName),
		url.QueryEscape(selector),
	)

	http.Redirect(w, r, restoreURL, http.StatusFound)
}

func (s *Server) handleAPISnapshots(w http.ResponseWriter, r *http.Request) {
	sess := SessionFromContext(r.Context())
	if sess == nil {
		http.Error(w, "unauthorized", http.StatusUnauthorized)
		return
	}

	snapshots, err := s.cfg.Store.ListUserSnapshots(r.Context(), sess.UserID)
	if err != nil {
		http.Error(w, fmt.Sprintf("failed to query snapshots: %v", err), http.StatusInternalServerError)
		return
	}

	w.Header().Set("Content-Type", "application/json")
	if err := json.NewEncoder(w).Encode(snapshots); err != nil {
		http.Error(w, err.Error(), http.StatusInternalServerError)
		return
	}
}
