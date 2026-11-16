package rocketleague

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
	"time"

	"github.com/dank/rlapi"
)

func TestPinnedRLAPIProtocolDefaults(t *testing.T) {
	data, err := os.ReadFile(filepath.Join("..", "..", "protocol-defaults.json"))
	if err != nil {
		t.Fatal(err)
	}
	var expected struct {
		GameVersion string `json:"game_version"`
		FeatureSet  string `json:"feature_set"`
		BuildSecret string `json:"build_secret"`
	}
	if err := json.Unmarshal(data, &expected); err != nil {
		t.Fatal(err)
	}
	client := rlapi.NewPsyNet()
	gameVersion, featureSet := client.GetVersion()
	if gameVersion != expected.GameVersion {
		t.Fatalf("got game version %q", gameVersion)
	}
	if featureSet != expected.FeatureSet {
		t.Fatalf("got feature set %q", featureSet)
	}
	if secret := client.GetBuildSecret(); secret != expected.BuildSecret {
		t.Fatalf("got build secret %q", secret)
	}
}

func TestExpiredRefreshTokenIsRedactedAndRequiresAuthorization(t *testing.T) {
	const secret = "credential-that-must-not-be-logged"
	external := errors.New(
		"invalid_refresh_token: refresh token '" + secret + "' is invalid: TOKEN_EXPIRED",
	)
	safe := redactError(external, secret)
	wrapped := fmt.Errorf("%w: %v", ErrAuthorizationRequired, safe)

	if strings.Contains(wrapped.Error(), secret) {
		t.Fatalf("error exposed refresh token: %q", wrapped)
	}
	if !errors.Is(wrapped, ErrAuthorizationRequired) {
		t.Fatalf("error should require authorization: %v", wrapped)
	}
	if got := connectionFailureState(wrapped); got != "authorization_required" {
		t.Fatalf("got state %q", got)
	}
}

func TestOrdinaryConnectionFailureReconnects(t *testing.T) {
	if got := connectionFailureState(errors.New("network unavailable")); got != "reconnecting" {
		t.Fatalf("got state %q", got)
	}
}

func TestWriteSecretUsesRestrictedPermissionsAndReplacesValue(t *testing.T) {
	path := filepath.Join(t.TempDir(), "nested", "token")
	if err := WriteRefreshToken(path, "first"); err != nil {
		t.Fatal(err)
	}
	if err := WriteRefreshToken(path, "second"); err != nil {
		t.Fatal(err)
	}
	value, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if string(value) != "second\n" {
		t.Fatalf("got token file %q", value)
	}
	info, err := os.Stat(path)
	if err != nil {
		t.Fatal(err)
	}
	if runtime.GOOS != "windows" && info.Mode().Perm() != 0o600 {
		t.Fatalf("got permissions %o, want 600", info.Mode().Perm())
	}
}

func TestRenewalDelayIsBounded(t *testing.T) {
	if got := renewalDelay(30); got != time.Minute {
		t.Fatalf("got short renewal delay %s", got)
	}
	if got := renewalDelay(7 * 24 * 60 * 60); got != 12*time.Hour {
		t.Fatalf("got long renewal delay %s", got)
	}
}

func TestVisibleSkillRatingConvertsPsyNetMMR(t *testing.T) {
	tests := []struct {
		name        string
		internalMMR float64
		want        float64
	}{
		{name: "typical ranked rating", internalMMR: 46.2, want: 1024},
		{name: "rating baseline", internalMMR: 0, want: 100},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			if got := visibleSkillRating(test.internalMMR); got != test.want {
				t.Fatalf("visibleSkillRating(%f) = %f, want %f", test.internalMMR, got, test.want)
			}
		})
	}
}
