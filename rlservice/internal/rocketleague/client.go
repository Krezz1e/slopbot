package rocketleague

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"os"
	"path/filepath"
	"runtime"
	"sort"
	"strings"
	"sync"
	"time"

	"github.com/dank/rlapi"
)

const (
	requestTimeout          = 12 * time.Second
	skillRatingScale        = 20.0
	skillRatingBase         = 100.0
	defaultTournamentRegion = "USE"
	connectionErrorLogEvery = time.Hour
)

var ErrAuthorizationRequired = errors.New("Rocket League authorization required")
var ErrAuthorizationNotRequired = errors.New("Rocket League reauthorization is not required")

type Config struct {
	RefreshTokenFile string
	GameVersion      string
	FeatureSet       string
}

type Status struct {
	Connected bool   `json:"connected"`
	State     string `json:"state"`
}

type Authorization struct {
	VerificationURI string `json:"verification_uri"`
	UserCode        string `json:"user_code"`
	ExpiresAt       int64  `json:"expires_at"`
}

type Profile struct {
	PlayerID   string `json:"player_id"`
	PlayerName string `json:"player_name"`
}

type Skill struct {
	PlayerID string  `json:"player_id"`
	MMR      float64 `json:"mmr"`
	Tier     int     `json:"tier"`
	Division int     `json:"division"`
}

type ActivitySkill struct {
	PlayerID      string  `json:"player_id"`
	Playlist      int     `json:"playlist"`
	MMR           float64 `json:"mmr"`
	Tier          int     `json:"tier"`
	Division      int     `json:"division"`
	WinStreak     int     `json:"win_streak"`
	MatchesPlayed int     `json:"matches_played"`
}

type PlayerDetailSkill struct {
	PlayerID               string  `json:"player_id"`
	Playlist               int     `json:"playlist"`
	MMR                    float64 `json:"mmr"`
	Tier                   int     `json:"tier"`
	Division               int     `json:"division"`
	WinStreak              int     `json:"win_streak"`
	MatchesPlayed          int     `json:"matches_played"`
	PlacementMatchesPlayed int     `json:"placement_matches_played"`
}

type SeasonRewards struct {
	Level int `json:"level"`
	Wins  int `json:"wins"`
}

type ClubMember struct {
	PlayerID   string `json:"player_id"`
	PlayerName string `json:"player_name"`
	RoleID     int    `json:"role_id"`
}

type ClubBadge struct {
	Stat  string `json:"stat"`
	Badge int    `json:"badge"`
}

type ClubDetails struct {
	ID            int          `json:"id"`
	Name          string       `json:"name"`
	Tag           string       `json:"tag"`
	PrimaryColor  int          `json:"primary_color"`
	AccentColor   int          `json:"accent_color"`
	Title         string       `json:"title"`
	OwnerPlayerID string       `json:"owner_player_id"`
	Verified      bool         `json:"verified"`
	Members       []ClubMember `json:"members"`
	Badges        []ClubBadge  `json:"badges"`
}

type PlayerDetails struct {
	Skills  []PlayerDetailSkill `json:"skills"`
	Rewards SeasonRewards       `json:"rewards"`
	Club    *ClubDetails        `json:"club"`
}

type ShopPrice struct {
	CurrencyID int `json:"currency_id"`
	Amount     int `json:"amount"`
}

type ShopCost struct {
	CostID int         `json:"cost_id"`
	Prices []ShopPrice `json:"prices"`
}

type ShopAttribute struct {
	Key   string `json:"key"`
	Value string `json:"value"`
}

type ShopItem struct {
	Key         string          `json:"key"`
	ShopID      int             `json:"shop_id"`
	ShopName    string          `json:"shop_name"`
	ShopType    string          `json:"shop_type"`
	ShopItemID  int             `json:"shop_item_id"`
	Title       string          `json:"title"`
	Description string          `json:"description"`
	ImageURL    string          `json:"image_url"`
	StartDate   int64           `json:"start_date"`
	EndDate     *int64          `json:"end_date"`
	ProductIDs  []int           `json:"product_ids"`
	Attributes  []ShopAttribute `json:"attributes"`
	Costs       []ShopCost      `json:"costs"`
}

type Tournament struct {
	ID             int    `json:"id"`
	Title          string `json:"title"`
	StartTime      int    `json:"start_time"`
	TeamSize       int    `json:"team_size"`
	GameMode       int    `json:"game_mode"`
	MaxBracketSize int    `json:"max_bracket_size"`
	RankMin        int    `json:"rank_min"`
	RankMax        int    `json:"rank_max"`
	Region         string `json:"region"`
	ScheduleID     int    `json:"schedule_id"`
}

type TournamentSchedule struct {
	Region      string       `json:"region"`
	Tournaments []Tournament `json:"tournaments"`
}

type RankedPlaylist struct {
	ID     int    `json:"id"`
	NodeID string `json:"node_id"`
}

type Backend interface {
	Status() Status
	StartAuthorization() (Authorization, error)
	Profiles(context.Context, []string) ([]Profile, error)
	Skills(context.Context, int, []string) ([]Skill, error)
	Activity(context.Context, []string) ([]ActivitySkill, error)
	PlayerDetails(context.Context, string) (PlayerDetails, error)
	ActivePlaylists(context.Context) ([]RankedPlaylist, error)
	Shop(context.Context) ([]ShopItem, error)
	TournamentSchedule(context.Context, string) (TournamentSchedule, error)
}

type Client struct {
	config Config
	logger *slog.Logger

	mu          sync.RWMutex
	rpc         *rlapi.PsyNetRPC
	state       string
	requestSlot chan struct{}

	authorizationMu sync.Mutex
	authorization   *Authorization
}

func NewClient(config Config, logger *slog.Logger) *Client {
	return &Client{
		config:      config,
		logger:      logger,
		state:       "starting",
		requestSlot: make(chan struct{}, 1),
	}
}

func (c *Client) Run(ctx context.Context) {
	delay := time.Second
	lastErrorState := ""
	lastErrorLog := time.Time{}
	for {
		if ctx.Err() != nil {
			c.disconnect("stopped")
			return
		}
		c.setState("connecting")
		rpc, renewAfter, err := c.connect()
		if err != nil {
			state := connectionFailureState(err)
			now := time.Now()
			if state != lastErrorState || now.Sub(lastErrorLog) >= connectionErrorLogEvery {
				c.logger.Error("Rocket League connection failed", "err", err, "retry_in", delay)
				lastErrorState = state
				lastErrorLog = now
			} else {
				c.logger.Debug("Rocket League connection still unavailable", "state", state)
			}
			c.setState(state)
			if !wait(ctx, delay) {
				c.disconnect("stopped")
				return
			}
			delay = min(delay*2, 60*time.Second)
			continue
		}
		delay = time.Second
		lastErrorState = ""
		lastErrorLog = time.Time{}
		c.setRPC(rpc)
		c.logger.Info("Rocket League service authenticated")
		if c.maintainConnection(ctx, rpc, renewAfter) {
			_ = rpc.Close()
			c.disconnect("stopped")
			return
		}
		c.logger.Warn("Rocket League WebSocket disconnected; reconnecting")
		c.disconnect("reconnecting")
	}
}

func (c *Client) Status() Status {
	c.mu.RLock()
	defer c.mu.RUnlock()
	return Status{
		Connected: c.rpc != nil && c.rpc.IsConnected(),
		State:     c.state,
	}
}

func (c *Client) StartAuthorization() (Authorization, error) {
	if c.Status().State != "authorization_required" {
		return Authorization{}, ErrAuthorizationNotRequired
	}

	c.authorizationMu.Lock()
	defer c.authorizationMu.Unlock()
	if c.authorization != nil && time.Now().Unix() < c.authorization.ExpiresAt {
		return *c.authorization, nil
	}

	egs := rlapi.NewEGS()
	device, err := egs.AuthenticateWithDevice()
	if err != nil {
		return Authorization{}, fmt.Errorf("start device authorization: %w", err)
	}
	authorization := Authorization{
		VerificationURI: device.VerificationURI,
		UserCode:        device.UserCode,
		ExpiresAt:       time.Now().Add(time.Duration(device.ExpiresIn) * time.Second).Unix(),
	}
	c.authorization = &authorization
	go c.completeAuthorization(egs, device, authorization)
	return authorization, nil
}

func (c *Client) completeAuthorization(
	egs *rlapi.EGS,
	device *rlapi.DeviceAuthResponse,
	authorization Authorization,
) {
	token, err := egs.WaitForDeviceAuthorization(device)
	if err == nil && token.RefreshToken == "" {
		err = errors.New("Epic did not return a refresh token")
	}
	if err == nil {
		err = WriteRefreshToken(c.config.RefreshTokenFile, token.RefreshToken)
	}

	c.authorizationMu.Lock()
	if c.authorization != nil && c.authorization.UserCode == authorization.UserCode {
		c.authorization = nil
	}
	c.authorizationMu.Unlock()

	if err != nil {
		c.logger.Error("Rocket League device authorization failed", "err", err)
		return
	}
	c.setState("reconnecting")
	c.logger.Info("Rocket League authorization saved", "path", c.config.RefreshTokenFile)
}

func (c *Client) Profiles(ctx context.Context, playerIDs []string) ([]Profile, error) {
	if err := c.acquireRequestSlot(ctx); err != nil {
		return nil, err
	}
	defer c.releaseRequestSlot()
	rpc, err := c.connectedRPC()
	if err != nil {
		return nil, err
	}
	requestCtx, cancel := context.WithTimeout(ctx, requestTimeout)
	defer cancel()
	raw, err := safeProfiles(requestCtx, rpc, toPlayerIDs(playerIDs))
	if err != nil {
		return nil, fmt.Errorf("get profiles: %w", err)
	}
	profiles := make([]Profile, 0, len(raw))
	for _, profile := range raw {
		profiles = append(profiles, Profile{
			PlayerID:   profile.PlayerID,
			PlayerName: profile.PlayerName,
		})
	}
	return profiles, nil
}

func (c *Client) Skills(ctx context.Context, playlist int, playerIDs []string) ([]Skill, error) {
	if err := c.acquireRequestSlot(ctx); err != nil {
		return nil, err
	}
	defer c.releaseRequestSlot()
	rpc, err := c.connectedRPC()
	if err != nil {
		return nil, err
	}
	requestCtx, cancel := context.WithTimeout(ctx, requestTimeout)
	defer cancel()
	raw, err := safeSkills(requestCtx, rpc, toPlayerIDs(playerIDs))
	if err != nil {
		return nil, fmt.Errorf("get skills: %w", err)
	}
	result := make([]Skill, 0, len(raw))
	for _, player := range raw {
		for _, skill := range player.Skills {
			if skill.Playlist != playlist {
				continue
			}
			result = append(result, Skill{
				PlayerID: player.PlayerID.String(),
				MMR:      visibleSkillRating(skill.MMR),
				Tier:     skill.Tier,
				Division: skill.Division,
			})
			break
		}
	}
	return result, nil
}

func (c *Client) Activity(ctx context.Context, playerIDs []string) ([]ActivitySkill, error) {
	if err := c.acquireRequestSlot(ctx); err != nil {
		return nil, err
	}
	defer c.releaseRequestSlot()
	rpc, err := c.connectedRPC()
	if err != nil {
		return nil, err
	}
	requestCtx, cancel := context.WithTimeout(ctx, requestTimeout)
	defer cancel()
	raw, err := safeSkills(requestCtx, rpc, toPlayerIDs(playerIDs))
	if err != nil {
		return nil, fmt.Errorf("get activity: %w", err)
	}
	result := make([]ActivitySkill, 0)
	for _, player := range raw {
		for _, skill := range player.Skills {
			result = append(result, ActivitySkill{
				PlayerID:      player.PlayerID.String(),
				Playlist:      skill.Playlist,
				MMR:           visibleSkillRating(skill.MMR),
				Tier:          skill.Tier,
				Division:      skill.Division,
				WinStreak:     skill.WinStreak,
				MatchesPlayed: skill.MatchesPlayed,
			})
		}
	}
	return result, nil
}

func (c *Client) PlayerDetails(ctx context.Context, playerID string) (PlayerDetails, error) {
	if err := c.acquireRequestSlot(ctx); err != nil {
		return PlayerDetails{}, err
	}
	defer c.releaseRequestSlot()
	rpc, err := c.connectedRPC()
	if err != nil {
		return PlayerDetails{}, err
	}
	requestCtx, cancel := context.WithTimeout(ctx, requestTimeout)
	defer cancel()
	raw, err := safePlayerSkill(requestCtx, rpc, rlapi.PlayerID(playerID))
	if err != nil {
		return PlayerDetails{}, fmt.Errorf("get player details: %w", err)
	}
	if raw == nil {
		return PlayerDetails{}, errors.New("get player details: empty response")
	}
	result := PlayerDetails{
		Skills: make([]PlayerDetailSkill, 0, len(raw.Skills)),
		Rewards: SeasonRewards{
			Level: raw.RewardLevels.SeasonLevel,
			Wins:  raw.RewardLevels.SeasonLevelWins,
		},
	}
	for _, skill := range raw.Skills {
		result.Skills = append(result.Skills, PlayerDetailSkill{
			PlayerID:               playerID,
			Playlist:               skill.Playlist,
			MMR:                    visibleSkillRating(skill.MMR),
			Tier:                   skill.Tier,
			Division:               skill.Division,
			WinStreak:              skill.WinStreak,
			MatchesPlayed:          skill.MatchesPlayed,
			PlacementMatchesPlayed: skill.PlacementMatchesPlayed,
		})
	}
	club, clubErr := safePlayerClubDetails(requestCtx, rpc, rlapi.PlayerID(playerID))
	if clubErr != nil {
		c.logger.Warn("Could not read optional Rocket League club details", "player_id", playerID, "err", clubErr)
		return result, nil
	}
	if club == nil || club.ClubID <= 0 {
		return result, nil
	}
	members := make([]ClubMember, 0, len(club.Members))
	for _, member := range club.Members {
		members = append(members, ClubMember{
			PlayerID: member.PlayerID.String(), PlayerName: member.PlayerName, RoleID: member.RoleID,
		})
	}
	badges := make([]ClubBadge, 0, len(club.Badges))
	for _, badge := range club.Badges {
		badges = append(badges, ClubBadge{Stat: badge.Stat, Badge: badge.Badge})
	}
	result.Club = &ClubDetails{
		ID: int(club.ClubID), Name: club.ClubName, Tag: club.ClubTag,
		PrimaryColor: club.PrimaryColor, AccentColor: club.AccentColor,
		Title: club.EquippedTitle, OwnerPlayerID: club.OwnerPlayerID.String(),
		Verified: club.Verified, Members: members, Badges: badges,
	}
	return result, nil
}

func (c *Client) ActivePlaylists(ctx context.Context) ([]RankedPlaylist, error) {
	if err := c.acquireRequestSlot(ctx); err != nil {
		return nil, err
	}
	defer c.releaseRequestSlot()
	rpc, err := c.connectedRPC()
	if err != nil {
		return nil, err
	}
	requestCtx, cancel := context.WithTimeout(ctx, requestTimeout)
	defer cancel()
	raw, err := safeActivePlaylists(requestCtx, rpc)
	if err != nil {
		return nil, fmt.Errorf("get active playlists: %w", err)
	}
	if raw == nil {
		return nil, errors.New("get active playlists: empty response")
	}
	playlists := make([]RankedPlaylist, 0, len(raw.RankedPlaylists))
	seen := make(map[int]struct{}, len(raw.RankedPlaylists))
	for _, playlist := range raw.RankedPlaylists {
		if playlist.Playlist <= 0 {
			continue
		}
		if _, exists := seen[playlist.Playlist]; exists {
			continue
		}
		seen[playlist.Playlist] = struct{}{}
		playlists = append(playlists, RankedPlaylist{
			ID:     playlist.Playlist,
			NodeID: playlist.NodeID,
		})
	}
	sort.Slice(playlists, func(i, j int) bool { return playlists[i].ID < playlists[j].ID })
	return playlists, nil
}

func (c *Client) Shop(ctx context.Context) ([]ShopItem, error) {
	if err := c.acquireRequestSlot(ctx); err != nil {
		return nil, err
	}
	defer c.releaseRequestSlot()
	rpc, err := c.connectedRPC()
	if err != nil {
		return nil, err
	}
	requestCtx, cancel := context.WithTimeout(ctx, requestTimeout)
	defer cancel()
	shopsResponse, err := safeStandardShops(requestCtx, rpc)
	if err != nil {
		return nil, fmt.Errorf("get standard shops: %w", err)
	}
	if shopsResponse == nil {
		return nil, errors.New("get standard shops: empty response")
	}
	shopIDs := make([]rlapi.ShopID, 0, len(shopsResponse.Shops))
	shops := make(map[rlapi.ShopID]rlapi.Shop, len(shopsResponse.Shops))
	for _, shop := range shopsResponse.Shops {
		shopIDs = append(shopIDs, shop.ID)
		shops[shop.ID] = shop
	}
	if len(shopIDs) == 0 {
		return []ShopItem{}, nil
	}
	catalogueResponse, err := safeShopCatalogue(requestCtx, rpc, shopIDs)
	if err != nil {
		return nil, fmt.Errorf("get shop catalogue: %w", err)
	}
	if catalogueResponse == nil {
		return nil, errors.New("get shop catalogue: empty response")
	}
	result := make([]ShopItem, 0)
	for _, catalogue := range catalogueResponse.Catalogues {
		shop := shops[catalogue.ShopID]
		for _, item := range catalogue.ShopItems {
			attributes := normalizeAttributes(item.Attributes)
			productIDs := make([]int, 0, len(item.DeliverableProducts))
			for _, deliverable := range item.DeliverableProducts {
				productIDs = append(productIDs, deliverable.Product.ProductID)
				attributes = append(attributes, normalizeAttributes(deliverable.Product.Attributes)...)
			}
			sort.Ints(productIDs)
			sort.Slice(attributes, func(i, j int) bool {
				if attributes[i].Key == attributes[j].Key {
					return attributes[i].Value < attributes[j].Value
				}
				return attributes[i].Key < attributes[j].Key
			})
			costs := make([]ShopCost, 0, len(item.Costs))
			for _, cost := range item.Costs {
				prices := make([]ShopPrice, 0, len(cost.Price))
				for _, price := range cost.Price {
					prices = append(prices, ShopPrice{CurrencyID: price.ID, Amount: price.Amount})
				}
				sort.Slice(prices, func(i, j int) bool {
					if prices[i].CurrencyID == prices[j].CurrencyID {
						return prices[i].Amount < prices[j].Amount
					}
					return prices[i].CurrencyID < prices[j].CurrencyID
				})
				costs = append(costs, ShopCost{CostID: cost.ShopItemCostID, Prices: prices})
			}
			sort.Slice(costs, func(i, j int) bool { return costs[i].CostID < costs[j].CostID })
			result = append(result, ShopItem{
				Key:         fmt.Sprintf("%d:%d", catalogue.ShopID, item.ShopItemID),
				ShopID:      int(catalogue.ShopID),
				ShopName:    stringValue(shop.Title, stringValue(shop.Name, shop.Type)),
				ShopType:    shop.Type,
				ShopItemID:  item.ShopItemID,
				Title:       stringValue(item.Title, fmt.Sprintf("Shop item %d", item.ShopItemID)),
				Description: stringValue(item.Description, ""),
				ImageURL:    stringValue(item.ImageURL, ""),
				StartDate:   item.StartDate,
				EndDate:     item.EndDate,
				ProductIDs:  productIDs,
				Attributes:  attributes,
				Costs:       costs,
			})
		}
	}
	sort.Slice(result, func(i, j int) bool { return result[i].Key < result[j].Key })
	return result, nil
}

// visibleSkillRating converts PsyNet's internal matchmaking value to the
// rating shown by Rocket League and commonly referred to by players as MMR.
func visibleSkillRating(internalMMR float64) float64 {
	return internalMMR*skillRatingScale + skillRatingBase
}

func (c *Client) TournamentSchedule(
	ctx context.Context,
	region string,
) (TournamentSchedule, error) {
	if err := c.acquireRequestSlot(ctx); err != nil {
		return TournamentSchedule{}, err
	}
	defer c.releaseRequestSlot()
	rpc, err := c.connectedRPC()
	if err != nil {
		return TournamentSchedule{}, err
	}
	resolvedRegion := strings.ToUpper(strings.TrimSpace(region))
	if resolvedRegion == "" {
		regionCtx, cancelRegion := context.WithTimeout(ctx, requestTimeout/3)
		resolvedRegion, err = safeTournamentScheduleRegion(regionCtx, rpc)
		cancelRegion()
		resolvedRegion = strings.ToUpper(strings.TrimSpace(resolvedRegion))
		if err != nil || resolvedRegion == "" {
			c.logger.Warn(
				"Could not detect tournament schedule region; using fallback",
				"fallback_region", defaultTournamentRegion,
				"err", err,
			)
			resolvedRegion = defaultTournamentRegion
		}
	}
	requestCtx, cancel := context.WithTimeout(ctx, requestTimeout)
	defer cancel()
	raw, err := safeTournamentSchedule(requestCtx, rpc, resolvedRegion)
	if err != nil {
		return TournamentSchedule{}, fmt.Errorf("get tournament schedule: %w", err)
	}
	result := TournamentSchedule{Region: resolvedRegion, Tournaments: []Tournament{}}
	seen := make(map[[2]int]struct{})
	for _, slot := range raw {
		for _, tournament := range slot.Tournaments {
			if tournament.ID <= 0 || tournament.StartTime <= 0 {
				continue
			}
			key := [2]int{tournament.ID, tournament.StartTime}
			if _, exists := seen[key]; exists {
				continue
			}
			seen[key] = struct{}{}
			result.Tournaments = append(result.Tournaments, Tournament{
				ID:             tournament.ID,
				Title:          tournament.Title,
				StartTime:      tournament.StartTime,
				TeamSize:       tournament.TeamSize,
				GameMode:       tournament.GameMode,
				MaxBracketSize: tournament.MaxBracketSize,
				RankMin:        tournament.RankMin,
				RankMax:        tournament.RankMax,
				Region:         tournament.Region,
				ScheduleID:     slot.ScheduleID,
			})
		}
	}
	sort.Slice(result.Tournaments, func(left, right int) bool {
		if result.Tournaments[left].StartTime != result.Tournaments[right].StartTime {
			return result.Tournaments[left].StartTime < result.Tournaments[right].StartTime
		}
		return result.Tournaments[left].ID < result.Tournaments[right].ID
	})
	return result, nil
}

func (c *Client) connect() (*rlapi.PsyNetRPC, time.Duration, error) {
	token, err := c.refreshCredentials()
	if err != nil {
		return nil, 0, err
	}
	psyNet := rlapi.NewPsyNet()
	psyNet.SetLogger(c.logger)
	if c.config.GameVersion != "" || c.config.FeatureSet != "" {
		if c.config.GameVersion == "" || c.config.FeatureSet == "" {
			return nil, 0, errors.New("game version and feature set overrides must be provided together")
		}
		psyNet.SetVersion(c.config.GameVersion, c.config.FeatureSet)
	}
	rpc, err := psyNet.AuthPlayer(token.AccessToken, token.AccountID, "")
	if err != nil {
		return nil, 0, fmt.Errorf("authenticate PsyNet: %w", err)
	}
	return rpc, renewalDelay(token.RefreshExpiresIn), nil
}

func (c *Client) refreshCredentials() (*rlapi.EOSTokenResponse, error) {
	refreshToken, err := readSecret(c.config.RefreshTokenFile)
	if err != nil {
		return nil, err
	}
	egs := rlapi.NewEGS()
	token, err := egs.RefreshEOSToken(refreshToken)
	if err != nil {
		safeError := redactError(err, refreshToken)
		if authorizationRequired(safeError) {
			return nil, fmt.Errorf("%w: %v", ErrAuthorizationRequired, safeError)
		}
		return nil, fmt.Errorf("refresh EOS token: %w", safeError)
	}
	if token.RefreshToken == "" {
		return nil, errors.New("EOS response did not include a refresh token")
	}
	if err := WriteRefreshToken(c.config.RefreshTokenFile, token.RefreshToken); err != nil {
		return nil, fmt.Errorf("persist rotated refresh token: %w", err)
	}
	return token, nil
}

func connectionFailureState(err error) string {
	if errors.Is(err, ErrAuthorizationRequired) {
		return "authorization_required"
	}
	return "reconnecting"
}

func authorizationRequired(err error) bool {
	message := strings.ToLower(err.Error())
	return strings.Contains(message, "invalid_refresh_token") ||
		strings.Contains(message, "token_expired") ||
		strings.Contains(message, "invalid_grant")
}

func redactError(err error, secrets ...string) error {
	message := err.Error()
	for _, secret := range secrets {
		if secret != "" {
			message = strings.ReplaceAll(message, secret, "[redacted]")
		}
	}
	return errors.New(message)
}

func (c *Client) connectedRPC() (*rlapi.PsyNetRPC, error) {
	c.mu.RLock()
	defer c.mu.RUnlock()
	if c.rpc == nil || !c.rpc.IsConnected() {
		return nil, errors.New("Rocket League service is not connected")
	}
	return c.rpc, nil
}

func (c *Client) setRPC(rpc *rlapi.PsyNetRPC) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.rpc = rpc
	c.state = "ready"
}

func (c *Client) setState(state string) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.state = state
}

func (c *Client) disconnect(state string) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.rpc = nil
	c.state = state
}

func (c *Client) acquireRequestSlot(ctx context.Context) error {
	select {
	case c.requestSlot <- struct{}{}:
		return nil
	case <-ctx.Done():
		return ctx.Err()
	}
}

func (c *Client) releaseRequestSlot() {
	<-c.requestSlot
}

func safeProfiles(
	ctx context.Context,
	rpc *rlapi.PsyNetRPC,
	playerIDs []rlapi.PlayerID,
) (profiles []rlapi.PlayerData, err error) {
	defer func() {
		if recovered := recover(); recovered != nil {
			err = fmt.Errorf("RLAPI panic while reading profiles: %v", recovered)
		}
	}()
	return rpc.GetProfiles(ctx, playerIDs)
}

func safeSkills(
	ctx context.Context,
	rpc *rlapi.PsyNetRPC,
	playerIDs []rlapi.PlayerID,
) (skills []rlapi.PlayerWithSkills, err error) {
	defer func() {
		if recovered := recover(); recovered != nil {
			err = fmt.Errorf("RLAPI panic while reading skills: %v", recovered)
		}
	}()
	return rpc.GetPlayersSkills(ctx, playerIDs)
}

func safePlayerSkill(
	ctx context.Context,
	rpc *rlapi.PsyNetRPC,
	playerID rlapi.PlayerID,
) (result *rlapi.GetPlayerSkillResponse, err error) {
	defer func() {
		if recovered := recover(); recovered != nil {
			err = fmt.Errorf("RLAPI panic while reading player details: %v", recovered)
		}
	}()
	return rpc.GetPlayerSkill(ctx, playerID)
}

func safePlayerClubDetails(
	ctx context.Context,
	rpc *rlapi.PsyNetRPC,
	playerID rlapi.PlayerID,
) (club *rlapi.ClubDetails, err error) {
	defer func() {
		if recovered := recover(); recovered != nil {
			err = fmt.Errorf("RLAPI panic while reading club details: %v", recovered)
		}
	}()
	return rpc.GetPlayerClubDetails(ctx, playerID)
}

func safeActivePlaylists(
	ctx context.Context,
	rpc *rlapi.PsyNetRPC,
) (playlists *rlapi.GetActivePlaylistsResponse, err error) {
	defer func() {
		if recovered := recover(); recovered != nil {
			err = fmt.Errorf("RLAPI panic while reading active playlists: %v", recovered)
		}
	}()
	return rpc.GetActivePlaylists(ctx)
}

func safeStandardShops(
	ctx context.Context,
	rpc *rlapi.PsyNetRPC,
) (response *rlapi.GetStandardShopsResponse, err error) {
	defer func() {
		if recovered := recover(); recovered != nil {
			err = fmt.Errorf("RLAPI panic while reading shops: %v", recovered)
		}
	}()
	return rpc.GetStandardShops(ctx)
}

func safeShopCatalogue(
	ctx context.Context,
	rpc *rlapi.PsyNetRPC,
	shopIDs []rlapi.ShopID,
) (response *rlapi.GetShopCatalogueResponse, err error) {
	defer func() {
		if recovered := recover(); recovered != nil {
			err = fmt.Errorf("RLAPI panic while reading shop catalogue: %v", recovered)
		}
	}()
	return rpc.GetShopCatalogue(ctx, shopIDs)
}

func normalizeAttributes(values []rlapi.ProductAttribute) []ShopAttribute {
	result := make([]ShopAttribute, 0, len(values))
	for _, attribute := range values {
		value, ok := attribute.Value.(string)
		if !ok {
			encoded, err := json.Marshal(attribute.Value)
			if err != nil {
				value = fmt.Sprint(attribute.Value)
			} else {
				value = string(encoded)
			}
		}
		result = append(result, ShopAttribute{Key: attribute.Key, Value: value})
	}
	return result
}

func stringValue(value *string, fallback string) string {
	if value == nil || strings.TrimSpace(*value) == "" {
		return fallback
	}
	return strings.TrimSpace(*value)
}

func safeTournamentScheduleRegion(
	ctx context.Context,
	rpc *rlapi.PsyNetRPC,
) (region string, err error) {
	defer func() {
		if recovered := recover(); recovered != nil {
			err = fmt.Errorf("RLAPI panic while reading tournament region: %v", recovered)
		}
	}()
	return rpc.GetTournamentScheduleRegion(ctx)
}

func safeTournamentSchedule(
	ctx context.Context,
	rpc *rlapi.PsyNetRPC,
	region string,
) (schedule []rlapi.TournamentSchedule, err error) {
	defer func() {
		if recovered := recover(); recovered != nil {
			err = fmt.Errorf("RLAPI panic while reading tournament schedule: %v", recovered)
		}
	}()
	return rpc.GetTournamentSchedule(ctx, region)
}

func toPlayerIDs(values []string) []rlapi.PlayerID {
	result := make([]rlapi.PlayerID, 0, len(values))
	for _, value := range values {
		result = append(result, rlapi.PlayerID(value))
	}
	return result
}

func readSecret(path string) (string, error) {
	value, err := os.ReadFile(path)
	if err != nil {
		return "", fmt.Errorf("read refresh token file: %w", err)
	}
	token := strings.TrimSpace(string(value))
	if token == "" {
		return "", errors.New("refresh token file is empty")
	}
	return token, nil
}

func WriteRefreshToken(path, value string) error {
	directory := filepath.Dir(path)
	if err := os.MkdirAll(directory, 0o700); err != nil {
		return err
	}
	temporary, err := os.CreateTemp(directory, ".rlapi-refresh-token-*")
	if err != nil {
		return err
	}
	temporaryPath := temporary.Name()
	defer os.Remove(temporaryPath)
	if err := temporary.Chmod(0o600); err != nil {
		temporary.Close()
		return err
	}
	if _, err := temporary.WriteString(value + "\n"); err != nil {
		temporary.Close()
		return err
	}
	if err := temporary.Sync(); err != nil {
		temporary.Close()
		return err
	}
	if err := temporary.Close(); err != nil {
		return err
	}
	if err := os.Rename(temporaryPath, path); err != nil {
		return err
	}
	// Windows does not support syncing a directory handle. The production target is Linux,
	// where syncing the containing directory makes the atomic rename durable.
	if runtime.GOOS == "windows" {
		return nil
	}
	directoryHandle, err := os.Open(directory)
	if err != nil {
		return err
	}
	defer directoryHandle.Close()
	return directoryHandle.Sync()
}

func wait(ctx context.Context, delay time.Duration) bool {
	timer := time.NewTimer(delay)
	defer timer.Stop()
	select {
	case <-ctx.Done():
		return false
	case <-timer.C:
		return true
	}
}

func (c *Client) maintainConnection(
	ctx context.Context,
	rpc *rlapi.PsyNetRPC,
	renewAfter time.Duration,
) bool {
	timer := time.NewTimer(renewAfter)
	defer timer.Stop()
	for {
		select {
		case <-ctx.Done():
			return true
		case event := <-rpc.Events():
			if event != nil && event.Type == rlapi.EventTypeDisconnected {
				return false
			}
		case <-timer.C:
			token, err := c.refreshCredentials()
			if err != nil {
				c.logger.Error(
					"Rocket League credential renewal failed; retrying",
					"err", err,
				)
				timer.Reset(time.Minute)
				continue
			}
			c.logger.Info("Rocket League credentials renewed")
			timer.Reset(renewalDelay(token.RefreshExpiresIn))
		}
	}
}

func renewalDelay(refreshExpiresIn int) time.Duration {
	if refreshExpiresIn <= 0 {
		return 6 * time.Hour
	}
	delay := time.Duration(refreshExpiresIn) * time.Second / 2
	if delay > 12*time.Hour {
		return 12 * time.Hour
	}
	if delay < time.Minute {
		return time.Minute
	}
	return delay
}
