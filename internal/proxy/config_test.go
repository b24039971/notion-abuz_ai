package proxy

import (
	"os"
	"path/filepath"
	"testing"
)

func TestLoadConfig_MissingFile(t *testing.T) {
	cfg, err := LoadConfig("non-existent-file.yaml")
	if err != nil {
		t.Fatalf("LoadConfig failed for missing file: %v", err)
	}
	if cfg.Server.Port != "8081" {
		t.Errorf("Expected default port 8081, got %s", cfg.Server.Port)
	}
}

func TestLoadConfig_ValidFile(t *testing.T) {
	content := []byte(`
server:
  port: "9000"
proxy:
  default_model: "custom-model"
`)
	tmpDir := t.TempDir()
	configPath := filepath.Join(tmpDir, "config.yaml")
	if err := os.WriteFile(configPath, content, 0644); err != nil {
		t.Fatal(err)
	}

	cfg, err := LoadConfig(configPath)
	if err != nil {
		t.Fatalf("LoadConfig failed: %v", err)
	}
	if cfg.Server.Port != "9000" {
		t.Errorf("Expected port 9000, got %s", cfg.Server.Port)
	}
	if cfg.Proxy.DefaultModel != "custom-model" {
		t.Errorf("Expected default_model custom-model, got %s", cfg.Proxy.DefaultModel)
	}
	if cfg.Server.AccountsDir != "accounts" {
		t.Errorf("Expected default accounts_dir 'accounts', got %s", cfg.Server.AccountsDir)
	}
}

func TestLoadConfig_EnvOverrides(t *testing.T) {
	os.Setenv("PORT", "9999")
	defer os.Unsetenv("PORT")

	cfg, err := LoadConfig("")
	if err != nil {
		t.Fatalf("LoadConfig failed: %v", err)
	}

	if cfg.Server.Port != "9999" {
		t.Errorf("Expected port 9999, got %s", cfg.Server.Port)
	}
}

func TestLoadConfig_InvalidFile(t *testing.T) {
	content := []byte(`
server:
  port: "9000
  invalid_yaml
`)
	tmpDir := t.TempDir()
	configPath := filepath.Join(tmpDir, "config.yaml")
	if err := os.WriteFile(configPath, content, 0644); err != nil {
		t.Fatal(err)
	}

	_, err := LoadConfig(configPath)
	if err == nil {
		t.Fatal("Expected LoadConfig to fail on invalid YAML")
	}
}

func TestLoadConfig_ModelMapDefaults(t *testing.T) {
	// Tests that ModelMap remains set if config.yaml is parsed but has no model_map
	content := []byte(`
server:
  port: "9000"
`)
	tmpDir := t.TempDir()
	configPath := filepath.Join(tmpDir, "config.yaml")
	if err := os.WriteFile(configPath, content, 0644); err != nil {
		t.Fatal(err)
	}

	cfg, err := LoadConfig(configPath)
	if err != nil {
		t.Fatalf("LoadConfig failed: %v", err)
	}

	if cfg.ModelMap == nil {
		t.Fatal("Expected ModelMap to not be nil")
	}
	if len(cfg.ModelMap) == 0 {
		t.Fatal("Expected ModelMap to have default values")
	}
}

func TestLoadConfig_ExtensiveEnvOverrides(t *testing.T) {
	t.Setenv("NOTION_API_BASE", "https://custom.notion.local")
	t.Setenv("INFERENCE_TIMEOUT", "999")
	t.Setenv("USER_AGENT", "TestAgent/1.0")
	t.Setenv("ENABLE_WEB_SEARCH", "false")

	content := []byte(`
proxy:
  notion_api_base: "https://file.notion.local"
  enable_web_search: true
timeouts:
  inference_timeout: 100
browser:
  user_agent: "FileAgent/1.0"
`)
	tmpDir := t.TempDir()
	configPath := filepath.Join(tmpDir, "config.yaml")
	if err := os.WriteFile(configPath, content, 0644); err != nil {
		t.Fatal(err)
	}

	cfg, err := LoadConfig(configPath)
	if err != nil {
		t.Fatalf("LoadConfig failed: %v", err)
	}

	if cfg.Proxy.NotionAPIBase != "https://custom.notion.local" {
		t.Errorf("Expected NotionAPIBase https://custom.notion.local, got %s", cfg.Proxy.NotionAPIBase)
	}
	if cfg.Timeouts.InferenceTimeout != 999 {
		t.Errorf("Expected InferenceTimeout 999, got %d", cfg.Timeouts.InferenceTimeout)
	}
	if cfg.Browser.UserAgent != "TestAgent/1.0" {
		t.Errorf("Expected UserAgent TestAgent/1.0, got %s", cfg.Browser.UserAgent)
	}
	if cfg.Proxy.EnableWebSearch == nil || *cfg.Proxy.EnableWebSearch != false {
		t.Errorf("Expected EnableWebSearch false, got %v", cfg.Proxy.EnableWebSearch)
	}
}

func TestLoadConfig_DefaultsWhenNoEnvOrFile(t *testing.T) {
	// Use t.Setenv to clear them safely for the duration of the test,
	// in case the host running the tests actually had them set.
	// t.Setenv with empty string effectively unsets them for the logic tested here,
	// or we can just rely on the test environment being relatively clean.
	// To strictly unset and restore, we'll manually implement a restore.
	origBase, baseSet := os.LookupEnv("NOTION_API_BASE")
	origAgent, agentSet := os.LookupEnv("USER_AGENT")
	origTimeout, timeoutSet := os.LookupEnv("INFERENCE_TIMEOUT")

	os.Unsetenv("NOTION_API_BASE")
	os.Unsetenv("USER_AGENT")
	os.Unsetenv("INFERENCE_TIMEOUT")

	t.Cleanup(func() {
		if baseSet {
			os.Setenv("NOTION_API_BASE", origBase)
		} else {
			os.Unsetenv("NOTION_API_BASE")
		}
		if agentSet {
			os.Setenv("USER_AGENT", origAgent)
		} else {
			os.Unsetenv("USER_AGENT")
		}
		if timeoutSet {
			os.Setenv("INFERENCE_TIMEOUT", origTimeout)
		} else {
			os.Unsetenv("INFERENCE_TIMEOUT")
		}
	})

	// Load with empty path so it skips file
	cfg, err := LoadConfig("")
	if err != nil {
		t.Fatalf("LoadConfig failed: %v", err)
	}

	expectedDefaults := DefaultConfig()

	if cfg.Proxy.NotionAPIBase != expectedDefaults.Proxy.NotionAPIBase {
		t.Errorf("Expected default NotionAPIBase %s, got %s", expectedDefaults.Proxy.NotionAPIBase, cfg.Proxy.NotionAPIBase)
	}
	if cfg.Timeouts.InferenceTimeout != expectedDefaults.Timeouts.InferenceTimeout {
		t.Errorf("Expected default InferenceTimeout %d, got %d", expectedDefaults.Timeouts.InferenceTimeout, cfg.Timeouts.InferenceTimeout)
	}
	if cfg.Browser.UserAgent != expectedDefaults.Browser.UserAgent {
		t.Errorf("Expected default UserAgent %s, got %s", expectedDefaults.Browser.UserAgent, cfg.Browser.UserAgent)
	}

	// Test the specific pointer default value too
	if cfg.Proxy.EnableWebSearch == nil || *cfg.Proxy.EnableWebSearch != true {
		t.Errorf("Expected default EnableWebSearch true, got %v", cfg.Proxy.EnableWebSearch)
	}
}
