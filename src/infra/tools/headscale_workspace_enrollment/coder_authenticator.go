// Verifies Coder owner sessions, completed workspace builds, and registered workspace agent tokens.

package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"time"

	"github.com/coder/websocket"
)

const maxCoderResponse = 2 << 20

type coderClient struct {
	baseURL string
	client  *http.Client
}

type coderWorkspace struct {
	ID          string              `json:"id"`
	IsPrebuild  bool                `json:"is_prebuild"`
	LatestBuild coderWorkspaceBuild `json:"latest_build"`
	Name        string              `json:"name"`
	OwnerID     string              `json:"owner_id"`
}

type coderWorkspaceBuild struct {
	ID               string                   `json:"id"`
	Job              coderProvisionerJob      `json:"job"`
	Resources        []coderWorkspaceResource `json:"resources"`
	Status           string                   `json:"status"`
	Transition       string                   `json:"transition"`
	WorkspaceID      string                   `json:"workspace_id"`
	WorkspaceOwnerID string                   `json:"workspace_owner_id"`
}

type coderProvisionerJob struct {
	Status string `json:"status"`
}

type coderWorkspaceResource struct {
	Agents []coderWorkspaceAgent `json:"agents"`
}

type coderWorkspaceAgent struct {
	ID       string  `json:"id"`
	Name     string  `json:"name"`
	ParentID *string `json:"parent_id"`
}

type agentTokenValidator interface {
	validate(context.Context, string) error
}

type coderWebsocketValidator struct {
	baseURL string
	client  *http.Client
	timeout time.Duration
}

func (a coderClient) authenticateOwner(ctx context.Context, token string) (coderUser, error) {
	if token == "" {
		return coderUser{}, errors.New("missing Coder session token")
	}
	var owner coderUser
	if err := a.get(ctx, token, "/api/v2/users/me", &owner); err != nil {
		return coderUser{}, err
	}
	if !userID.MatchString(owner.ID) || owner.Email == "" {
		return coderUser{}, errors.New("Coder returned an invalid owner")
	}
	var claims struct {
		Claims map[string]any `json:"claims"`
	}
	if err := a.get(ctx, token, "/api/v2/users/oidc-claims", &claims); err != nil {
		return coderUser{}, err
	}
	owner.Issuer, _ = claims.Claims["iss"].(string)
	owner.Subject, _ = claims.Claims["sub"].(string)
	owner.PreferredUsername, _ = claims.Claims["preferred_username"].(string)
	owner.Groups = claimStrings(claims.Claims["groups"])
	if owner.Issuer == "" || owner.Subject == "" || !loginName.MatchString(owner.PreferredUsername) {
		return coderUser{}, errors.New("Coder returned invalid OIDC claims")
	}
	return owner, nil
}

func (a coderClient) workspace(ctx context.Context, token, workspaceID string) (coderWorkspace, error) {
	if !userID.MatchString(workspaceID) {
		return coderWorkspace{}, errors.New("invalid workspace ID")
	}
	var workspace coderWorkspace
	if err := a.get(ctx, token, "/api/v2/workspaces/"+workspaceID, &workspace); err != nil {
		return coderWorkspace{}, err
	}
	return workspace, nil
}

func (a coderClient) get(ctx context.Context, token, path string, destination any) error {
	baseURL, err := url.Parse(a.baseURL)
	if err != nil || baseURL.Scheme == "" || baseURL.Host == "" {
		return errors.New("invalid Coder URL")
	}
	baseURL.Path = strings.TrimRight(baseURL.Path, "/") + path
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, baseURL.String(), nil)
	if err != nil {
		return err
	}
	req.Header.Set("Coder-Session-Token", token)
	res, err := a.httpClient().Do(req)
	if err != nil {
		return err
	}
	defer res.Body.Close()
	if res.StatusCode != http.StatusOK {
		return fmt.Errorf("Coder returned %s", res.Status)
	}
	return decodeBoundedJSON(res.Body, maxCoderResponse, destination)
}

func (a coderClient) httpClient() *http.Client {
	if a.client != nil {
		return a.client
	}
	return boundedHTTPClient(upstreamTimeout)
}

func (v coderWebsocketValidator) validate(ctx context.Context, token string) error {
	if token == "" {
		return errors.New("missing Coder agent token")
	}
	endpoint, err := url.Parse(v.baseURL)
	if err != nil || endpoint.Scheme == "" || endpoint.Host == "" {
		return errors.New("invalid Coder URL")
	}
	endpoint.Path = strings.TrimRight(endpoint.Path, "/") + "/api/v2/workspaceagents/me/rpc"
	query := endpoint.Query()
	query.Set("role", "snapshot-broker")
	query.Set("version", "2.10")
	endpoint.RawQuery = query.Encode()
	timeout := v.timeout
	if timeout <= 0 {
		timeout = upstreamTimeout
	}
	boundedContext, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	client := v.client
	if client == nil {
		client = boundedHTTPClient(timeout)
	}
	connection, _, err := websocket.Dial(boundedContext, endpoint.String(), &websocket.DialOptions{
		HTTPClient: client,
		HTTPHeader: http.Header{"Coder-Session-Token": []string{token}},
	})
	if err != nil {
		return errors.New("Coder rejected the workspace agent token")
	}
	return connection.CloseNow()
}

func claimStrings(value any) []string {
	values, ok := value.([]any)
	if !ok {
		return nil
	}
	result := make([]string, 0, len(values))
	for _, value := range values {
		text, ok := value.(string)
		if !ok || text == "" {
			return nil
		}
		result = append(result, text)
	}
	return result
}

func decodeBoundedJSON(reader io.Reader, limit int64, destination any) error {
	data, err := io.ReadAll(io.LimitReader(reader, limit+1))
	if err != nil {
		return err
	}
	if int64(len(data)) > limit {
		return errors.New("JSON response exceeds its size limit")
	}
	decoder := json.NewDecoder(bytes.NewReader(data))
	if err := decoder.Decode(destination); err != nil {
		return err
	}
	var extra any
	if err := decoder.Decode(&extra); err != io.EOF {
		return errors.New("JSON response contains trailing data")
	}
	return nil
}
