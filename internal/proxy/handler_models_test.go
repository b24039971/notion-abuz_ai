package proxy

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"reflect"
	"testing"
)

func TestHandlePublicModels_IncludesConfiguredAliases(t *testing.T) {
	original := SnapshotModelMap()
	ReplaceModelMap(map[string]string{
		"opus-4.6":      "avocado-froyo-medium",
		"opus-4.8-high": "avocado-froyo-medium", // multiple aliases point to the same ID
		"gpt-5.4":       "oval-kumquat-medium",
		"claude-3-test": "test-internal-id",
	})
	t.Cleanup(func() {
		ReplaceModelMap(original)
	})

	pool := NewAccountPool()
	pool.accounts = []*Account{
		{
			Models: []ModelEntry{
				{Name: "GPT 5.4", ID: "oval-kumquat-medium"},
				{Name: "Opus 4.6", ID: "avocado-froyo-medium"},
				{Name: "Unknown Internal", ID: "unknown-internal-id"},
			},
		},
	}

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/v1/models", nil)
	HandlePublicModels(pool).ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", rec.Code)
	}

	var resp publicModelResponse
	if err := json.Unmarshal(rec.Body.Bytes(), &resp); err != nil {
		t.Fatalf("decode response: %v", err)
	}

	gotIDs := make([]string, 0, len(resp.Data))
	for _, item := range resp.Data {
		gotIDs = append(gotIDs, item.ID)
	}

	// Should contain "gpt-5.4", "opus-4.6", "opus-4.8-high", and "unknown-internal"
	wantIDs := []string{"gpt-5.4", "opus-4.6", "opus-4.8-high", "unknown-internal"}
	if !reflect.DeepEqual(gotIDs, wantIDs) {
		t.Fatalf("unexpected model ids: got %v want %v", gotIDs, wantIDs)
	}
}

func TestHandlePublicModels_UsesPoolModelsAndNormalizesIDs(t *testing.T) {
	original := SnapshotModelMap()
	ReplaceModelMap(map[string]string{
		"opus-4.6": "avocado-froyo-medium",
		"gpt-5.4":  "oval-kumquat-medium",
	})
	t.Cleanup(func() {
		ReplaceModelMap(original)
	})

	pool := NewAccountPool()
	pool.accounts = []*Account{
		{
			Models: []ModelEntry{
				{Name: "GPT 5.4", ID: "oval-kumquat-medium"},
				{Name: "Opus 4.6", ID: "avocado-froyo-medium"},
				{Name: "GPT 5.4", ID: "oval-kumquat-medium"},
			},
		},
	}

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/v1/models", nil)
	HandlePublicModels(pool).ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d: %s", rec.Code, rec.Body.String())
	}

	var resp publicModelResponse
	if err := json.Unmarshal(rec.Body.Bytes(), &resp); err != nil {
		t.Fatalf("decode response: %v", err)
	}

	if resp.Object != "list" {
		t.Fatalf("expected object=list, got %q", resp.Object)
	}

	gotIDs := make([]string, 0, len(resp.Data))
	for _, item := range resp.Data {
		if item.Object != "model" {
			t.Fatalf("expected object=model, got %q", item.Object)
		}
		if item.Created != publicModelCreatedAt {
			t.Fatalf("expected created=%d, got %d", publicModelCreatedAt, item.Created)
		}
		if item.OwnedBy != "notion-manager" {
			t.Fatalf("expected owned_by notion-manager, got %q", item.OwnedBy)
		}
		gotIDs = append(gotIDs, item.ID)
	}

	wantIDs := []string{"gpt-5.4", "opus-4.6"}
	if !reflect.DeepEqual(gotIDs, wantIDs) {
		t.Fatalf("unexpected model ids: got %v want %v", gotIDs, wantIDs)
	}
}

func TestHandlePublicModels_FallsBackToDefaultModelMap(t *testing.T) {
	original := SnapshotModelMap()
	ReplaceModelMap(map[string]string{
		"gemini-2.5-flash": "vertex-gemini-2.5-flash",
		"sonnet-4.6":       "almond-croissant-low",
	})
	t.Cleanup(func() {
		ReplaceModelMap(original)
	})

	pool := NewAccountPool()

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/models", nil)
	HandlePublicModels(pool).ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d: %s", rec.Code, rec.Body.String())
	}

	var resp publicModelResponse
	if err := json.Unmarshal(rec.Body.Bytes(), &resp); err != nil {
		t.Fatalf("decode response: %v", err)
	}

	gotIDs := make([]string, 0, len(resp.Data))
	for _, item := range resp.Data {
		gotIDs = append(gotIDs, item.ID)
	}

	wantIDs := []string{"gemini-2.5-flash", "sonnet-4.6"}
	if !reflect.DeepEqual(gotIDs, wantIDs) {
		t.Fatalf("unexpected fallback models: got %v want %v", gotIDs, wantIDs)
	}
}

func TestHandlePublicModels_MethodNotAllowed(t *testing.T) {
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, "/v1/models", nil)
	HandlePublicModels(NewAccountPool()).ServeHTTP(rec, req)

	if rec.Code != http.StatusMethodNotAllowed {
		t.Fatalf("expected 405, got %d: %s", rec.Code, rec.Body.String())
	}
	if allow := rec.Header().Get("Allow"); allow != http.MethodGet {
		t.Fatalf("expected Allow=GET, got %q", allow)
	}
}

func TestPublicModelID(t *testing.T) {
	tests := []struct {
		name       string
		modelName  string
		internalID string
		mapSetup   map[string]string
		want       string
	}{
		{
			name:       "normalizes display name",
			modelName:  "GPT 5.4",
			internalID: "oval-kumquat-medium",
			want:       "gpt-5.4",
		},
		{
			name:       "falls back to internal ID lookup if name is empty",
			modelName:  "",
			internalID: "oval-kumquat-medium",
			mapSetup: map[string]string{
				"gpt-5.4-fallback": "oval-kumquat-medium",
			},
			want: "gpt-5.4-fallback",
		},
		{
			name:       "returns empty string if name is empty and ID not in map",
			modelName:  "",
			internalID: "unknown-id",
			mapSetup:   map[string]string{},
			want:       "",
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			if tc.mapSetup != nil {
				original := SnapshotModelMap()
				ReplaceModelMap(tc.mapSetup)
				t.Cleanup(func() {
					ReplaceModelMap(original)
				})
			}

			model := ModelEntry{Name: tc.modelName, ID: tc.internalID}
			got := publicModelID(model)
			if got != tc.want {
				t.Errorf("publicModelID() = %q; want %q", got, tc.want)
			}
		})
	}
}

func TestNormalizeModelName(t *testing.T) {
	tests := []struct {
		in   string
		want string
	}{
		{"", ""},
		{"GPT 5.4", "gpt-5.4"},
		{"  Opus 4.6  ", "opus-4.6"},
		{"Claude 3 Test", "claude-3-test"},
	}
	for _, tc := range tests {
		t.Run(tc.in, func(t *testing.T) {
			got := normalizeModelName(tc.in)
			if got != tc.want {
				t.Errorf("normalizeModelName(%q) = %q; want %q", tc.in, got, tc.want)
			}
		})
	}
}

func TestHandlePublicModels_UsesNormalizedNames(t *testing.T) {
	original := SnapshotModelMap()
	ReplaceModelMap(map[string]string{
		"opus-4.6": "avocado-froyo-medium", // keep this for fallback checks if needed
	})
	t.Cleanup(func() {
		ReplaceModelMap(original)
	})

	pool := NewAccountPool()
	pool.accounts = []*Account{
		{
			Models: []ModelEntry{
				{Name: "  GPT 5.4  ", ID: "oval-kumquat-medium"},
				{Name: "Claude 3 Opus", ID: "avocado-froyo-medium"},
				{Name: "Weird_Model_Name!", ID: "weird-id"},
			},
		},
	}

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/v1/models", nil)
	HandlePublicModels(pool).ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", rec.Code)
	}

	var resp publicModelResponse
	if err := json.Unmarshal(rec.Body.Bytes(), &resp); err != nil {
		t.Fatalf("decode response: %v", err)
	}

	gotIDs := make([]string, 0, len(resp.Data))
	for _, item := range resp.Data {
		gotIDs = append(gotIDs, item.ID)
	}

	// Should contain "gpt-5.4", "claude-3-opus", "weird_model_name!", and "opus-4.6" (from configured aliases fallback)
	wantIDs := []string{"claude-3-opus", "gpt-5.4", "opus-4.6", "weird_model_name!"}
	if !reflect.DeepEqual(gotIDs, wantIDs) {
		t.Fatalf("unexpected model ids: got %v want %v", gotIDs, wantIDs)
	}
}

func TestHandleAdminModels(t *testing.T) {
	original := SnapshotModelMap()
	ReplaceModelMap(map[string]string{
		"opus-test-alias": "avocado-froyo-medium",
	})
	t.Cleanup(func() {
		ReplaceModelMap(original)
	})

	pool := NewAccountPool()
	pool.accounts = []*Account{
		{
			Models: []ModelEntry{
				{Name: "GPT 5.4", ID: "oval-kumquat-medium"},
				{Name: "Opus 4.6", ID: "avocado-froyo-medium"},
			},
		},
	}

	auth := NewDashboardAuth("", "test-salt") // empty password means no auth required

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/admin/models", nil)

	HandleAdminModels(pool, auth).ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", rec.Code)
	}

	var resp struct {
		ModelMap        map[string]string `json:"model_map"`
		AvailableModels []ModelEntry      `json:"available_models"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &resp); err != nil {
		t.Fatalf("decode response: %v", err)
	}

	if resp.ModelMap["opus-test-alias"] != "avocado-froyo-medium" {
		t.Errorf("expected model_map to contain opus-test-alias -> avocado-froyo-medium, got %v", resp.ModelMap)
	}

	if len(resp.AvailableModels) != 2 {
		t.Errorf("expected 2 available_models, got %d", len(resp.AvailableModels))
	}
}
