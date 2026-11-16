package api

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/krezz1e/slopbot/rlservice/internal/rocketleague"
)

const testToken = "0123456789abcdef0123456789abcdef"

type fakeBackend struct {
	status              *rocketleague.Status
	activePlaylistCalls *int
	activePlaylistErr   error
	activePlaylists     []rocketleague.RankedPlaylist
	activitySkills      []rocketleague.ActivitySkill
}

func (fakeBackend) StartAuthorization() (rocketleague.Authorization, error) {
	return rocketleague.Authorization{
		VerificationURI: "https://www.epicgames.com/activate",
		UserCode:        "ABCDEFGH",
		ExpiresAt:       1_800_000_000,
	}, nil
}

func (backend fakeBackend) Status() rocketleague.Status {
	if backend.status != nil {
		return *backend.status
	}
	return rocketleague.Status{Connected: true, State: "ready"}
}

func (fakeBackend) Profiles(_ context.Context, playerIDs []string) ([]rocketleague.Profile, error) {
	return []rocketleague.Profile{{PlayerID: playerIDs[0], PlayerName: "Player One"}}, nil
}

func (fakeBackend) Skills(
	_ context.Context,
	_ int,
	playerIDs []string,
) ([]rocketleague.Skill, error) {
	return []rocketleague.Skill{{
		PlayerID: playerIDs[0], MMR: 1111.4, Tier: 14, Division: 2,
	}}, nil
}

func (backend fakeBackend) Activity(
	_ context.Context,
	playerIDs []string,
) ([]rocketleague.ActivitySkill, error) {
	if backend.activitySkills != nil {
		return backend.activitySkills, nil
	}
	return []rocketleague.ActivitySkill{{
		PlayerID: playerIDs[0], Playlist: 11, MMR: 1111.4, Tier: 14, Division: 2,
		WinStreak: 3, MatchesPlayed: 81,
	}}, nil
}

func (fakeBackend) PlayerDetails(
	_ context.Context,
	playerID string,
) (rocketleague.PlayerDetails, error) {
	return rocketleague.PlayerDetails{
		Skills: []rocketleague.PlayerDetailSkill{{
			PlayerID: playerID, Playlist: 11, MMR: 1111.4, Tier: 14, Division: 2,
			WinStreak: 3, MatchesPlayed: 81, PlacementMatchesPlayed: 10,
		}},
		Rewards: rocketleague.SeasonRewards{Level: 6, Wins: 4},
		Club: &rocketleague.ClubDetails{
			ID: 7, Name: "Sloppers", Tag: "SLOP", PrimaryColor: 255,
			AccentColor: 65280, Title: "Champions", OwnerPlayerID: playerID, Verified: true,
			Members: []rocketleague.ClubMember{{
				PlayerID: playerID, PlayerName: "Player One", RoleID: 1,
			}},
			Badges: []rocketleague.ClubBadge{{Stat: "Goal", Badge: 2}},
		},
	}, nil
}

func (backend fakeBackend) ActivePlaylists(
	_ context.Context,
) ([]rocketleague.RankedPlaylist, error) {
	if backend.activePlaylistCalls != nil {
		*backend.activePlaylistCalls++
	}
	if backend.activePlaylistErr != nil {
		return nil, backend.activePlaylistErr
	}
	if backend.activePlaylists != nil {
		return backend.activePlaylists, nil
	}
	return []rocketleague.RankedPlaylist{
		{ID: 10, NodeID: "RankedDuel"},
		{ID: 11, NodeID: "RankedDoubles"},
		{ID: 13, NodeID: "RankedStandard"},
	}, nil
}

func (fakeBackend) Shop(_ context.Context) ([]rocketleague.ShopItem, error) {
	endDate := int64(200)
	return []rocketleague.ShopItem{{
		Key: "1:2", ShopID: 1, ShopName: "Featured", ShopType: "Featured",
		ShopItemID: 2, Title: "Octane Decal", StartDate: 100, EndDate: &endDate,
		ProductIDs: []int{42},
		Attributes: []rocketleague.ShopAttribute{{Key: "Painted", Value: "Crimson"}},
		Costs: []rocketleague.ShopCost{{
			CostID: 3, Prices: []rocketleague.ShopPrice{{CurrencyID: 1, Amount: 700}},
		}},
	}}, nil
}

func (fakeBackend) TournamentSchedule(
	_ context.Context,
	region string,
) (rocketleague.TournamentSchedule, error) {
	if region == "" {
		region = "USE"
	}
	return rocketleague.TournamentSchedule{
		Region: region,
		Tournaments: []rocketleague.Tournament{{
			ID: 42, Title: "2v2 Soccar", StartTime: 1_756_843_200, TeamSize: 2,
		}},
	}, nil
}

func TestStatusRequiresAuthentication(t *testing.T) {
	request := httptest.NewRequest(http.MethodGet, "/v1/status", nil)
	response := httptest.NewRecorder()

	NewServer(fakeBackend{}, testToken).ServeHTTP(response, request)

	if response.Code != http.StatusUnauthorized {
		t.Fatalf("got status %d, want %d", response.Code, http.StatusUnauthorized)
	}
}

func TestReadinessRequiresAuthenticatedConnection(t *testing.T) {
	request := httptest.NewRequest(http.MethodGet, "/readyz", nil)
	response := httptest.NewRecorder()

	NewServer(fakeBackend{}, testToken).ServeHTTP(response, request)

	if response.Code != http.StatusOK || response.Body.String() != "{\"status\":\"ready\"}\n" {
		t.Fatalf("got status %d: %s", response.Code, response.Body.String())
	}
}

func TestReadinessAllowsDeploymentWhileAuthorizationIsRequired(t *testing.T) {
	request := httptest.NewRequest(http.MethodGet, "/readyz", nil)
	response := httptest.NewRecorder()
	status := rocketleague.Status{State: "authorization_required"}

	NewServer(fakeBackend{status: &status}, testToken).ServeHTTP(response, request)

	if response.Code != http.StatusPreconditionRequired {
		t.Fatalf("got status %d: %s", response.Code, response.Body.String())
	}
}

func TestAuthorizationRequiresServiceToken(t *testing.T) {
	server := NewServer(fakeBackend{}, testToken)
	unauthorized := httptest.NewRequest(http.MethodPost, "/v1/authorization", nil)
	unauthorizedResponse := httptest.NewRecorder()
	server.ServeHTTP(unauthorizedResponse, unauthorized)
	if unauthorizedResponse.Code != http.StatusUnauthorized {
		t.Fatalf("got status %d, want %d", unauthorizedResponse.Code, http.StatusUnauthorized)
	}

	request := httptest.NewRequest(http.MethodPost, "/v1/authorization", nil)
	request.Header.Set("Authorization", "Bearer "+testToken)
	response := httptest.NewRecorder()
	server.ServeHTTP(response, request)
	if response.Code != http.StatusAccepted {
		t.Fatalf("got status %d: %s", response.Code, response.Body.String())
	}
}

func TestPlaylistsAreDiscoveredAndCached(t *testing.T) {
	calls := 0
	server := NewServer(fakeBackend{
		activePlaylistCalls: &calls,
		activePlaylists: []rocketleague.RankedPlaylist{
			{ID: 99, NodeID: "RankedLimitedTime"},
			{ID: 11, NodeID: "RankedDoubles"},
			{ID: 99, NodeID: "Duplicate"},
		},
	}, testToken)

	for range 2 {
		request := httptest.NewRequest(http.MethodGet, "/v1/playlists", nil)
		request.Header.Set("Authorization", "Bearer "+testToken)
		response := httptest.NewRecorder()
		server.ServeHTTP(response, request)
		if response.Code != http.StatusOK {
			t.Fatalf("got status %d: %s", response.Code, response.Body.String())
		}
		var payload playlistResponse
		if err := json.Unmarshal(response.Body.Bytes(), &payload); err != nil {
			t.Fatal(err)
		}
		if payload.Fallback {
			t.Fatal("unexpected fallback playlist response")
		}
		if len(payload.RankedPlaylists) != 2 || payload.RankedPlaylists[0].ID != 11 ||
			payload.RankedPlaylists[1].ID != 99 {
			t.Fatalf("unexpected ranked playlists: %#v", payload.RankedPlaylists)
		}
	}
	if calls != 1 {
		t.Fatalf("got %d active-playlist calls, want 1", calls)
	}
}

func TestPlaylistsFallBackWhenDiscoveryFails(t *testing.T) {
	server := NewServer(fakeBackend{activePlaylistErr: errors.New("unavailable")}, testToken)
	request := httptest.NewRequest(http.MethodGet, "/v1/playlists", nil)
	request.Header.Set("Authorization", "Bearer "+testToken)
	response := httptest.NewRecorder()

	server.ServeHTTP(response, request)

	var payload playlistResponse
	if err := json.Unmarshal(response.Body.Bytes(), &payload); err != nil {
		t.Fatal(err)
	}
	if !payload.Fallback || len(payload.RankedPlaylists) != len(fallbackRankedPlaylists) {
		t.Fatalf("unexpected fallback response: %#v", payload)
	}
}

func TestActivityUsesDiscoveredRankedPlaylists(t *testing.T) {
	server := NewServer(fakeBackend{
		activePlaylists: []rocketleague.RankedPlaylist{{ID: 99, NodeID: "RankedLimitedTime"}},
		activitySkills: []rocketleague.ActivitySkill{
			{PlayerID: "Epic|abc123|0", Playlist: 11, MatchesPlayed: 1},
			{PlayerID: "Epic|abc123|0", Playlist: 99, MatchesPlayed: 2},
		},
	}, testToken)
	body := bytes.NewBufferString(`{"player_ids":["Epic|abc123|0"]}`)
	request := httptest.NewRequest(http.MethodPost, "/v1/activity", body)
	request.Header.Set("Authorization", "Bearer "+testToken)
	response := httptest.NewRecorder()

	server.ServeHTTP(response, request)

	var payload struct {
		Activity []activityResponse `json:"activity"`
	}
	if err := json.Unmarshal(response.Body.Bytes(), &payload); err != nil {
		t.Fatal(err)
	}
	if len(payload.Activity) != 1 || payload.Activity[0].Playlist != 99 {
		t.Fatalf("unexpected activity response: %#v", payload.Activity)
	}
}

func TestSkillsReturnsMMRAndRank(t *testing.T) {
	body := bytes.NewBufferString(`{"playlist":11,"player_ids":["Epic|abc123|0"]}`)
	request := httptest.NewRequest(http.MethodPost, "/v1/skills", body)
	request.Header.Set("Authorization", "Bearer "+testToken)
	response := httptest.NewRecorder()

	NewServer(fakeBackend{}, testToken).ServeHTTP(response, request)

	if response.Code != http.StatusOK {
		t.Fatalf("got status %d: %s", response.Code, response.Body.String())
	}
	var payload struct {
		Skills []skillResponse `json:"skills"`
	}
	if err := json.Unmarshal(response.Body.Bytes(), &payload); err != nil {
		t.Fatal(err)
	}
	if len(payload.Skills) != 1 {
		t.Fatalf("got %d skills, want 1", len(payload.Skills))
	}
	if payload.Skills[0].MMR != 1111.4 {
		t.Fatalf("got MMR %f", payload.Skills[0].MMR)
	}
	if payload.Skills[0].Rank != "Diamond II, Division III" {
		t.Fatalf("got rank %q", payload.Skills[0].Rank)
	}
}

func TestSkillsRejectsMalformedPlayerID(t *testing.T) {
	body := bytes.NewBufferString(`{"playlist":11,"player_ids":["Epic|abc|0|extra"]}`)
	request := httptest.NewRequest(http.MethodPost, "/v1/skills", body)
	request.Header.Set("Authorization", "Bearer "+testToken)
	response := httptest.NewRecorder()

	NewServer(fakeBackend{}, testToken).ServeHTTP(response, request)

	if response.Code != http.StatusBadRequest {
		t.Fatalf("got status %d, want %d", response.Code, http.StatusBadRequest)
	}
}

func TestActivityReturnsTrackingCounters(t *testing.T) {
	body := bytes.NewBufferString(`{"player_ids":["Epic|abc123|0"]}`)
	request := httptest.NewRequest(http.MethodPost, "/v1/activity", body)
	request.Header.Set("Authorization", "Bearer "+testToken)
	response := httptest.NewRecorder()

	NewServer(fakeBackend{}, testToken).ServeHTTP(response, request)

	if response.Code != http.StatusOK {
		t.Fatalf("got status %d: %s", response.Code, response.Body.String())
	}
	var payload struct {
		Activity []activityResponse `json:"activity"`
	}
	if err := json.Unmarshal(response.Body.Bytes(), &payload); err != nil {
		t.Fatal(err)
	}
	if len(payload.Activity) != 1 || payload.Activity[0].MatchesPlayed != 81 {
		t.Fatalf("unexpected activity: %#v", payload.Activity)
	}
	if payload.Activity[0].WinStreak != 3 {
		t.Fatalf("got streak %d", payload.Activity[0].WinStreak)
	}
}

func TestPlayerDetailsReturnsSeasonProgressAndClub(t *testing.T) {
	body := bytes.NewBufferString(`{"player_id":"Epic|abc123|0"}`)
	request := httptest.NewRequest(http.MethodPost, "/v1/player-details", body)
	request.Header.Set("Authorization", "Bearer "+testToken)
	response := httptest.NewRecorder()

	NewServer(fakeBackend{}, testToken).ServeHTTP(response, request)

	if response.Code != http.StatusOK {
		t.Fatalf("got status %d: %s", response.Code, response.Body.String())
	}
	var payload playerDetailsResponse
	if err := json.Unmarshal(response.Body.Bytes(), &payload); err != nil {
		t.Fatal(err)
	}
	if len(payload.Skills) != 1 || payload.Skills[0].PlacementMatchesPlayed != 10 {
		t.Fatalf("unexpected skills: %#v", payload.Skills)
	}
	if payload.Skills[0].Rank != "Diamond II, Division III" {
		t.Fatalf("unexpected rank: %q", payload.Skills[0].Rank)
	}
	if payload.Rewards.Level != 6 || payload.Rewards.Wins != 4 {
		t.Fatalf("unexpected rewards: %#v", payload.Rewards)
	}
	if payload.Club == nil || payload.Club.Tag != "SLOP" || len(payload.Club.Members) != 1 {
		t.Fatalf("unexpected club: %#v", payload.Club)
	}
}

func TestPlayerDetailsRejectsMalformedPlayerID(t *testing.T) {
	body := bytes.NewBufferString(`{"player_id":"Epic|abc|0|extra"}`)
	request := httptest.NewRequest(http.MethodPost, "/v1/player-details", body)
	request.Header.Set("Authorization", "Bearer "+testToken)
	response := httptest.NewRecorder()

	NewServer(fakeBackend{}, testToken).ServeHTTP(response, request)

	if response.Code != http.StatusBadRequest {
		t.Fatalf("got status %d, want %d", response.Code, http.StatusBadRequest)
	}
}

func TestShopReturnsNormalizedCatalogue(t *testing.T) {
	request := httptest.NewRequest(http.MethodGet, "/v1/shop", nil)
	request.Header.Set("Authorization", "Bearer "+testToken)
	response := httptest.NewRecorder()

	NewServer(fakeBackend{}, testToken).ServeHTTP(response, request)

	if response.Code != http.StatusOK {
		t.Fatalf("got status %d: %s", response.Code, response.Body.String())
	}
	var payload struct {
		Items []rocketleague.ShopItem `json:"items"`
	}
	if err := json.Unmarshal(response.Body.Bytes(), &payload); err != nil {
		t.Fatal(err)
	}
	if len(payload.Items) != 1 || payload.Items[0].Title != "Octane Decal" {
		t.Fatalf("unexpected shop items: %#v", payload.Items)
	}
	if payload.Items[0].Costs[0].Prices[0].Amount != 700 {
		t.Fatalf("unexpected shop price: %#v", payload.Items[0].Costs)
	}
}

func TestHealthDoesNotExposeConnectionDetails(t *testing.T) {
	request := httptest.NewRequest(http.MethodGet, "/healthz", nil)
	response := httptest.NewRecorder()

	NewServer(fakeBackend{}, testToken).ServeHTTP(response, request)

	if response.Code != http.StatusOK || response.Body.String() != "{\"status\":\"ok\"}\n" {
		t.Fatalf("unexpected response: %d %q", response.Code, response.Body.String())
	}
}

func TestTournamentsUsesOptionalRegion(t *testing.T) {
	request := httptest.NewRequest(http.MethodGet, "/v1/tournaments?region=eu", nil)
	request.Header.Set("Authorization", "Bearer "+testToken)
	response := httptest.NewRecorder()

	NewServer(fakeBackend{}, testToken).ServeHTTP(response, request)

	if response.Code != http.StatusOK {
		t.Fatalf("got status %d: %s", response.Code, response.Body.String())
	}
	var payload rocketleague.TournamentSchedule
	if err := json.Unmarshal(response.Body.Bytes(), &payload); err != nil {
		t.Fatal(err)
	}
	if payload.Region != "EU" || len(payload.Tournaments) != 1 {
		t.Fatalf("unexpected schedule: %#v", payload)
	}
}

func TestTournamentsRejectsMalformedRegion(t *testing.T) {
	request := httptest.NewRequest(http.MethodGet, "/v1/tournaments?region=us-east", nil)
	request.Header.Set("Authorization", "Bearer "+testToken)
	response := httptest.NewRecorder()

	NewServer(fakeBackend{}, testToken).ServeHTTP(response, request)

	if response.Code != http.StatusBadRequest {
		t.Fatalf("got status %d, want %d", response.Code, http.StatusBadRequest)
	}
}
