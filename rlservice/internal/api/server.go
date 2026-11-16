package api

import (
	"context"
	"crypto/subtle"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"sort"
	"strings"
	"sync"
	"time"

	"github.com/krezz1e/slopbot/rlservice/internal/rocketleague"
)

const (
	maxBodyBytes     = 1 << 20
	maxPlayers       = 500
	batchSize        = 50
	playlistCacheTTL = 10 * time.Minute
)

var allowedPlatforms = map[string]struct{}{
	"Epic": {}, "Steam": {}, "PS4": {}, "XboxOne": {}, "Switch": {},
}

var fallbackRankedPlaylists = []rocketleague.RankedPlaylist{
	{ID: 10, NodeID: "RankedDuel"},
	{ID: 11, NodeID: "RankedDoubles"},
	{ID: 13, NodeID: "RankedStandard"},
	{ID: 27, NodeID: "Hoops"},
	{ID: 28, NodeID: "Rumble"},
	{ID: 29, NodeID: "Dropshot"},
	{ID: 30, NodeID: "SnowDay"},
}

type Server struct {
	backend rocketleague.Backend
	token   string
	mux     *http.ServeMux

	playlistMu      sync.RWMutex
	playlistChecked time.Time
	playlistCatalog playlistResponse
}

type playlistResponse struct {
	RankedPlaylists []rocketleague.RankedPlaylist `json:"ranked_playlists"`
	Fallback        bool                          `json:"fallback"`
}

type playerRequest struct {
	PlayerIDs []string `json:"player_ids"`
}

type skillRequest struct {
	Playlist  int      `json:"playlist"`
	PlayerIDs []string `json:"player_ids"`
}

type playerDetailsRequest struct {
	PlayerID string `json:"player_id"`
}

type skillResponse struct {
	PlayerID string  `json:"player_id"`
	MMR      float64 `json:"mmr"`
	Tier     int     `json:"tier"`
	Division int     `json:"division"`
	Rank     string  `json:"rank"`
}

type playerDetailSkillResponse struct {
	PlayerID               string  `json:"player_id"`
	Playlist               int     `json:"playlist"`
	MMR                    float64 `json:"mmr"`
	Tier                   int     `json:"tier"`
	Division               int     `json:"division"`
	Rank                   string  `json:"rank"`
	WinStreak              int     `json:"win_streak"`
	MatchesPlayed          int     `json:"matches_played"`
	PlacementMatchesPlayed int     `json:"placement_matches_played"`
}

type playerDetailsResponse struct {
	Skills  []playerDetailSkillResponse `json:"skills"`
	Rewards rocketleague.SeasonRewards  `json:"rewards"`
	Club    *rocketleague.ClubDetails   `json:"club"`
}

type activityResponse struct {
	PlayerID      string  `json:"player_id"`
	Playlist      int     `json:"playlist"`
	MMR           float64 `json:"mmr"`
	Tier          int     `json:"tier"`
	Division      int     `json:"division"`
	Rank          string  `json:"rank"`
	WinStreak     int     `json:"win_streak"`
	MatchesPlayed int     `json:"matches_played"`
}

func NewServer(backend rocketleague.Backend, token string) *Server {
	server := &Server{backend: backend, token: token, mux: http.NewServeMux()}
	server.mux.HandleFunc("GET /healthz", server.health)
	server.mux.HandleFunc("GET /readyz", server.ready)
	server.mux.HandleFunc("GET /v1/status", server.authorize(server.status))
	server.mux.HandleFunc("POST /v1/authorization", server.authorize(server.authorization))
	server.mux.HandleFunc("POST /v1/profiles", server.authorize(server.profiles))
	server.mux.HandleFunc("POST /v1/skills", server.authorize(server.skills))
	server.mux.HandleFunc("POST /v1/activity", server.authorize(server.activity))
	server.mux.HandleFunc("POST /v1/player-details", server.authorize(server.playerDetails))
	server.mux.HandleFunc("GET /v1/playlists", server.authorize(server.playlists))
	server.mux.HandleFunc("GET /v1/shop", server.authorize(server.shop))
	server.mux.HandleFunc("GET /v1/tournaments", server.authorize(server.tournaments))
	return server
}

func (s *Server) playlists(response http.ResponseWriter, request *http.Request) {
	writeJSON(response, http.StatusOK, s.rankedPlaylists(request.Context()))
}

func (s *Server) shop(response http.ResponseWriter, request *http.Request) {
	items, err := s.backend.Shop(request.Context())
	if err != nil {
		writeError(response, http.StatusServiceUnavailable, "Rocket League is unavailable")
		return
	}
	writeJSON(response, http.StatusOK, map[string]any{"items": items})
}

func (s *Server) activity(response http.ResponseWriter, request *http.Request) {
	var payload playerRequest
	if err := decodeRequest(response, request, &payload); err != nil {
		writeError(response, http.StatusBadRequest, err.Error())
		return
	}
	playerIDs, err := validatePlayerIDs(payload.PlayerIDs)
	if err != nil {
		writeError(response, http.StatusBadRequest, err.Error())
		return
	}
	active := s.rankedPlaylists(request.Context())
	trackedPlaylists := make(map[int]struct{}, len(active.RankedPlaylists))
	for _, playlist := range active.RankedPlaylists {
		trackedPlaylists[playlist.ID] = struct{}{}
	}
	result := make([]activityResponse, 0)
	for start := 0; start < len(playerIDs); start += batchSize {
		end := min(start+batchSize, len(playerIDs))
		batch, fetchErr := s.backend.Activity(request.Context(), playerIDs[start:end])
		if fetchErr != nil {
			writeError(response, http.StatusServiceUnavailable, "Rocket League is unavailable")
			return
		}
		for _, skill := range batch {
			if _, tracked := trackedPlaylists[skill.Playlist]; !tracked {
				continue
			}
			result = append(result, activityResponse{
				PlayerID:      skill.PlayerID,
				Playlist:      skill.Playlist,
				MMR:           skill.MMR,
				Tier:          skill.Tier,
				Division:      skill.Division,
				Rank:          rankName(skill.Tier, skill.Division),
				WinStreak:     skill.WinStreak,
				MatchesPlayed: skill.MatchesPlayed,
			})
		}
	}
	writeJSON(response, http.StatusOK, map[string]any{"activity": result})
}

func (s *Server) playerDetails(response http.ResponseWriter, request *http.Request) {
	var payload playerDetailsRequest
	if err := decodeRequest(response, request, &payload); err != nil {
		writeError(response, http.StatusBadRequest, err.Error())
		return
	}
	playerIDs, err := validatePlayerIDs([]string{payload.PlayerID})
	if err != nil {
		writeError(response, http.StatusBadRequest, err.Error())
		return
	}
	details, err := s.backend.PlayerDetails(request.Context(), playerIDs[0])
	if err != nil {
		writeError(response, http.StatusServiceUnavailable, "Rocket League is unavailable")
		return
	}
	result := playerDetailsResponse{
		Skills:  make([]playerDetailSkillResponse, 0, len(details.Skills)),
		Rewards: details.Rewards,
		Club:    details.Club,
	}
	for _, skill := range details.Skills {
		result.Skills = append(result.Skills, playerDetailSkillResponse{
			PlayerID: skill.PlayerID, Playlist: skill.Playlist, MMR: skill.MMR,
			Tier: skill.Tier, Division: skill.Division, Rank: rankName(skill.Tier, skill.Division),
			WinStreak: skill.WinStreak, MatchesPlayed: skill.MatchesPlayed,
			PlacementMatchesPlayed: skill.PlacementMatchesPlayed,
		})
	}
	writeJSON(response, http.StatusOK, result)
}

func (s *Server) rankedPlaylists(ctx context.Context) playlistResponse {
	now := time.Now()
	s.playlistMu.RLock()
	if !s.playlistChecked.IsZero() && now.Sub(s.playlistChecked) < playlistCacheTTL {
		cached := clonePlaylistResponse(s.playlistCatalog)
		s.playlistMu.RUnlock()
		return cached
	}
	s.playlistMu.RUnlock()

	playlists, err := s.backend.ActivePlaylists(ctx)
	catalog := playlistResponse{RankedPlaylists: normalizePlaylists(playlists)}
	if err != nil || len(catalog.RankedPlaylists) == 0 {
		catalog = playlistResponse{
			RankedPlaylists: append([]rocketleague.RankedPlaylist(nil), fallbackRankedPlaylists...),
			Fallback:        true,
		}
	}
	s.playlistMu.Lock()
	s.playlistChecked = now
	s.playlistCatalog = clonePlaylistResponse(catalog)
	s.playlistMu.Unlock()
	return catalog
}

func normalizePlaylists(playlists []rocketleague.RankedPlaylist) []rocketleague.RankedPlaylist {
	result := make([]rocketleague.RankedPlaylist, 0, len(playlists))
	seen := make(map[int]struct{}, len(playlists))
	for _, playlist := range playlists {
		if playlist.ID <= 0 {
			continue
		}
		if _, exists := seen[playlist.ID]; exists {
			continue
		}
		seen[playlist.ID] = struct{}{}
		result = append(result, playlist)
	}
	sort.Slice(result, func(i, j int) bool { return result[i].ID < result[j].ID })
	return result
}

func clonePlaylistResponse(catalog playlistResponse) playlistResponse {
	return playlistResponse{
		RankedPlaylists: append([]rocketleague.RankedPlaylist(nil), catalog.RankedPlaylists...),
		Fallback:        catalog.Fallback,
	}
}

func (s *Server) ServeHTTP(response http.ResponseWriter, request *http.Request) {
	s.mux.ServeHTTP(response, request)
}

func (s *Server) health(response http.ResponseWriter, _ *http.Request) {
	writeJSON(response, http.StatusOK, map[string]string{"status": "ok"})
}

func (s *Server) ready(response http.ResponseWriter, _ *http.Request) {
	status := s.backend.Status()
	if status.State == "authorization_required" {
		writeError(response, http.StatusPreconditionRequired, "Rocket League authorization is required")
		return
	}
	if !status.Connected {
		writeError(response, http.StatusServiceUnavailable, "Rocket League is not authenticated")
		return
	}
	writeJSON(response, http.StatusOK, map[string]string{"status": "ready"})
}

func (s *Server) status(response http.ResponseWriter, _ *http.Request) {
	writeJSON(response, http.StatusOK, s.backend.Status())
}

func (s *Server) authorization(response http.ResponseWriter, _ *http.Request) {
	authorization, err := s.backend.StartAuthorization()
	if errors.Is(err, rocketleague.ErrAuthorizationNotRequired) {
		writeError(response, http.StatusConflict, err.Error())
		return
	}
	if err != nil {
		writeError(response, http.StatusServiceUnavailable, "Could not start Rocket League authorization")
		return
	}
	writeJSON(response, http.StatusAccepted, authorization)
}

func (s *Server) profiles(response http.ResponseWriter, request *http.Request) {
	var payload playerRequest
	if err := decodeRequest(response, request, &payload); err != nil {
		writeError(response, http.StatusBadRequest, err.Error())
		return
	}
	playerIDs, err := validatePlayerIDs(payload.PlayerIDs)
	if err != nil {
		writeError(response, http.StatusBadRequest, err.Error())
		return
	}
	profiles := make([]rocketleague.Profile, 0, len(playerIDs))
	for start := 0; start < len(playerIDs); start += batchSize {
		end := min(start+batchSize, len(playerIDs))
		batch, fetchErr := s.backend.Profiles(request.Context(), playerIDs[start:end])
		if fetchErr != nil {
			writeError(response, http.StatusServiceUnavailable, "Rocket League is unavailable")
			return
		}
		profiles = append(profiles, batch...)
	}
	writeJSON(response, http.StatusOK, map[string]any{"profiles": profiles})
}

func (s *Server) skills(response http.ResponseWriter, request *http.Request) {
	var payload skillRequest
	if err := decodeRequest(response, request, &payload); err != nil {
		writeError(response, http.StatusBadRequest, err.Error())
		return
	}
	if payload.Playlist <= 0 {
		writeError(response, http.StatusBadRequest, "playlist must be positive")
		return
	}
	playerIDs, err := validatePlayerIDs(payload.PlayerIDs)
	if err != nil {
		writeError(response, http.StatusBadRequest, err.Error())
		return
	}
	result := make([]skillResponse, 0, len(playerIDs))
	for start := 0; start < len(playerIDs); start += batchSize {
		end := min(start+batchSize, len(playerIDs))
		batch, fetchErr := s.backend.Skills(
			request.Context(), payload.Playlist, playerIDs[start:end],
		)
		if fetchErr != nil {
			writeError(response, http.StatusServiceUnavailable, "Rocket League is unavailable")
			return
		}
		for _, skill := range batch {
			result = append(result, skillResponse{
				PlayerID: skill.PlayerID,
				MMR:      skill.MMR,
				Tier:     skill.Tier,
				Division: skill.Division,
				Rank:     rankName(skill.Tier, skill.Division),
			})
		}
	}
	writeJSON(response, http.StatusOK, map[string]any{"skills": result})
}

func (s *Server) tournaments(response http.ResponseWriter, request *http.Request) {
	region := strings.ToUpper(strings.TrimSpace(request.URL.Query().Get("region")))
	if region != "" && !validTournamentRegion(region) {
		writeError(response, http.StatusBadRequest, "region must contain 1 to 8 ASCII letters")
		return
	}
	schedule, err := s.backend.TournamentSchedule(request.Context(), region)
	if err != nil {
		writeError(response, http.StatusServiceUnavailable, "Rocket League is unavailable")
		return
	}
	writeJSON(response, http.StatusOK, schedule)
}

func (s *Server) authorize(next http.HandlerFunc) http.HandlerFunc {
	return func(response http.ResponseWriter, request *http.Request) {
		provided := strings.TrimPrefix(request.Header.Get("Authorization"), "Bearer ")
		if len(provided) != len(s.token) || subtle.ConstantTimeCompare(
			[]byte(provided), []byte(s.token),
		) != 1 {
			writeError(response, http.StatusUnauthorized, "unauthorized")
			return
		}
		next(response, request)
	}
}

func decodeRequest(response http.ResponseWriter, request *http.Request, target any) error {
	request.Body = http.MaxBytesReader(response, request.Body, maxBodyBytes)
	decoder := json.NewDecoder(request.Body)
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(target); err != nil {
		return errors.New("request body must be valid JSON")
	}
	if err := decoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
		return errors.New("request body must contain one JSON object")
	}
	return nil
}

func validatePlayerIDs(values []string) ([]string, error) {
	if len(values) == 0 {
		return nil, errors.New("player_ids must not be empty")
	}
	if len(values) > maxPlayers {
		return nil, fmt.Errorf("player_ids may contain at most %d entries", maxPlayers)
	}
	seen := make(map[string]struct{}, len(values))
	result := make([]string, 0, len(values))
	for _, value := range values {
		parts := strings.Split(value, "|")
		if len(parts) != 3 || parts[1] == "" || parts[2] != "0" {
			return nil, errors.New("player_ids contains an invalid Rocket League player ID")
		}
		if _, ok := allowedPlatforms[parts[0]]; !ok {
			return nil, errors.New("player_ids contains an unsupported platform")
		}
		if len(parts[1]) > 128 {
			return nil, errors.New("player_ids contains an account ID that is too long")
		}
		if _, ok := seen[value]; ok {
			continue
		}
		seen[value] = struct{}{}
		result = append(result, value)
	}
	return result, nil
}

func validTournamentRegion(region string) bool {
	if len(region) == 0 || len(region) > 8 {
		return false
	}
	for _, character := range region {
		if character < 'A' || character > 'Z' {
			return false
		}
	}
	return true
}

func rankName(tier, division int) string {
	tiers := []string{
		"Unranked",
		"Bronze I", "Bronze II", "Bronze III",
		"Silver I", "Silver II", "Silver III",
		"Gold I", "Gold II", "Gold III",
		"Platinum I", "Platinum II", "Platinum III",
		"Diamond I", "Diamond II", "Diamond III",
		"Champion I", "Champion II", "Champion III",
		"Grand Champion I", "Grand Champion II", "Grand Champion III",
		"Supersonic Legend",
	}
	if tier <= 0 {
		return tiers[0]
	}
	if tier >= len(tiers) {
		return fmt.Sprintf("Tier %d, Division %d", tier, division+1)
	}
	return fmt.Sprintf("%s, Division %s", tiers[tier], romanDivision(division))
}

func romanDivision(division int) string {
	values := []string{"I", "II", "III", "IV"}
	if division >= 0 && division < len(values) {
		return values[division]
	}
	return fmt.Sprintf("%d", division+1)
}

func writeError(response http.ResponseWriter, status int, message string) {
	writeJSON(response, status, map[string]string{"error": message})
}

func writeJSON(response http.ResponseWriter, status int, payload any) {
	response.Header().Set("Content-Type", "application/json")
	response.Header().Set("X-Content-Type-Options", "nosniff")
	response.WriteHeader(status)
	_ = json.NewEncoder(response).Encode(payload)
}
