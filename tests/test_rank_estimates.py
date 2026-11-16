import pytest

from slopbot.database import SubscriptionStore
from slopbot.rank_estimates import (
    adjacent,
    calculate_rank_steps,
    rank_state,
    rank_target,
)
from slopbot.rocketleague import RocketLeagueSkill


def evidence(*, history: bool = True):
    return {
        "transitions": [],
        "boundaries": [
            (14, 1, 14, 2, "win", 1020.0, 2.0, 4, 4, 1000),
            (14, 1, 14, 0, "loss", 980.0, 2.0, 3, 3, 1000),
            (14, 3, 15, 0, "win", 1080.0, 3.0, 5, 5, 1000),
            (14, 0, 13, 3, "loss", 920.0, 3.0, 5, 5, 1000),
        ],
        "rank_samples": [],
        "has_personal_history": history,
        "personal_deltas": [
            ("win", 1000.0, 1010.0),
            ("win", 1010.0, 1020.0),
            ("win", 1020.0, 1030.0),
            ("loss", 1000.0, 990.0),
            ("loss", 990.0, 980.0),
            ("loss", 980.0, 970.0),
        ],
        "global_deltas": [],
    }


def test_rank_state_and_targets_follow_rocket_league_tiers():
    state = rank_state("Diamond II, Division II")
    assert state == (14, 1)
    assert adjacent(state, 1) == (14, 2)
    assert adjacent(state, -1) == (14, 0)
    assert rank_target(state, 1) == (15, 0)
    assert rank_target(state, -1) == (13, 3)
    assert rank_state("Unranked") is None


def test_exact_boundaries_and_personal_changes_produce_four_near_term_steps():
    skill = RocketLeagueSkill("p", 1000, 14, 1, "Diamond II, Division II", playlist=11)
    result = calculate_rank_steps(
        skill=skill, playlist=11, player_id="p", evidence=evidence(), observed_at=900
    )
    assert result is not None
    assert [(step.direction, step.kind, step.games) for step in result.steps] == [
        ("win", "division", 2),
        ("win", "rank", 8),
        ("loss", "division", 2),
        ("loss", "rank", 8),
    ]
    assert all(step.delta_source == "personal" for step in result.steps)


def test_complete_rank_step_history_is_used_when_mmr_changes_are_unavailable():
    observed = evidence(history=False)
    observed["personal_deltas"] = []
    skill = RocketLeagueSkill("new", 1000, 14, 1, "Diamond II, Division II", playlist=11)
    result = calculate_rank_steps(
        skill=skill, playlist=11, player_id="new", evidence=observed, observed_at=900
    )
    assert result is not None
    assert [(step.direction, step.kind, step.games, step.basis) for step in result.steps] == [
        ("win", "division", 2, "usual"),
        ("win", "rank", 9, "usual"),
        ("loss", "division", 2, "usual"),
        ("loss", "rank", 6, "usual"),
    ]


def test_placements_and_profiles_without_any_evidence_have_no_estimate():
    empty = evidence(history=False)
    empty.update(boundaries=[], personal_deltas=[])
    skill = RocketLeagueSkill("new", 1000, 14, 1, "Diamond II, Division II", playlist=11)
    result = calculate_rank_steps(
        skill=skill, playlist=11, player_id="new", evidence=empty, observed_at=900
    )
    assert result is not None and not result.steps
    placement = RocketLeagueSkill(
        "new", 1000, 14, 1, "Diamond II, Division II", placement_matches_played=4, playlist=11
    )
    assert (
        calculate_rank_steps(
            skill=placement, playlist=11, player_id="new", evidence=evidence(), observed_at=900
        )
        is None
    )


def test_reverse_crossings_and_opposite_result_deltas_fill_both_directions():
    observed = evidence()
    observed["boundaries"] = [
        (14, 2, 14, 1, "loss", 1020.0, 2.0, 4, 4, 1000),
        (15, 0, 14, 3, "loss", 1080.0, 3.0, 5, 5, 1000),
        (14, 1, 14, 0, "loss", 980.0, 2.0, 3, 3, 1000),
        (14, 0, 13, 3, "loss", 920.0, 3.0, 5, 5, 1000),
    ]
    observed["personal_deltas"] = [
        ("loss", 1030.0, 1020.0),
        ("loss", 1020.0, 1010.0),
        ("loss", 1010.0, 1000.0),
    ]
    skill = RocketLeagueSkill("p", 1000, 14, 1, "Diamond II, Division II", playlist=11)

    result = calculate_rank_steps(
        skill=skill, playlist=11, player_id="p", evidence=observed, observed_at=900
    )

    assert result is not None
    assert [(step.direction, step.kind) for step in result.steps] == [
        ("win", "division"),
        ("win", "rank"),
        ("loss", "division"),
        ("loss", "rank"),
    ]
    assert result.steps[0].games == 2


def test_nearby_rank_ranges_project_missing_upper_boundaries():
    observed = evidence()
    observed["boundaries"] = []
    observed["rank_samples"] = [
        (13, 3, 930.0),
        (13, 3, 935.0),
        (13, 3, 940.0),
        (14, 0, 950.0),
        (14, 0, 955.0),
        (14, 0, 960.0),
        (14, 1, 990.0),
        (14, 1, 1000.0),
        (14, 1, 1010.0),
    ]
    observed["personal_deltas"] = [
        ("loss", 1030.0, 1020.0),
        ("loss", 1020.0, 1010.0),
        ("loss", 1010.0, 1000.0),
    ]
    skill = RocketLeagueSkill("p", 1000, 14, 1, "Diamond II, Division II", playlist=11)

    result = calculate_rank_steps(
        skill=skill, playlist=11, player_id="p", evidence=observed, observed_at=900
    )

    assert result is not None
    assert [(step.direction, step.kind) for step in result.steps] == [
        ("win", "division"),
        ("win", "rank"),
        ("loss", "division"),
        ("loss", "rank"),
    ]
    assert all(step.basis == "boundary" for step in result.steps)


async def test_rank_crossing_persists_anonymous_boundary(tmp_path):
    store = SubscriptionStore(tmp_path / "boundaries.db")
    await store.initialize()
    common = dict(
        guild_id=1,
        discord_user_id=2,
        player_id="player",
        player_name="Player",
        threshold=3,
        inactivity_seconds=1800,
    )
    await store.record_rocket_league_activity_batch(
        **common,
        activities=[(11, 10, 1, 1000, "Diamond II, Division II")],
        rank_positions={11: (14, 1)},
        observed_at=100,
    )
    await store.record_rocket_league_activity_batch(
        **common,
        activities=[(11, 11, 2, 1010, "Diamond II, Division III")],
        rank_positions={11: (14, 2)},
        observed_at=200,
    )

    row = await store._run(
        lambda connection: connection.execute(
            "SELECT from_tier,from_division,to_tier,to_division,direction,"
            "mean_mmr,mean_results,result_sample_count,sample_count "
            "FROM rocket_league_rank_boundaries"
        ).fetchone()
    )
    assert tuple(row) == (14, 1, 14, 2, "win", 1005.0, 1.0, 1, 1)


@pytest.mark.parametrize(
    "kind,source,target,reverse,before,after,expected",
    [
        (
            "division",
            "Diamond II, Division II",
            "Diamond II, Division III",
            (14, 2, 14, 1, "loss", 1010.0, None, 0, 4, 1000),
            1025.0,
            1035.0,
            1030,
        ),
        (
            "rank",
            "Diamond II, Division IV",
            "Diamond III, Division I",
            (15, 0, 14, 3, "loss", 1060.0, None, 0, 4, 1000),
            1085.0,
            1095.0,
            1090,
        ),
    ],
)
def test_direct_crossing_beats_stored_reverse_boundary(
    kind, source, target, reverse, before, after, expected
):
    observed = evidence()
    observed["boundaries"] = [reverse]
    observed["transitions"] = [("rank_up", source, target, before, after)]
    skill = RocketLeagueSkill("p", 1000, 14, 1, "Diamond II, Division II", playlist=11)
    result = calculate_rank_steps(skill=skill, playlist=11, player_id="p", evidence=observed)
    assert result is not None
    step = next(s for s in result.steps if s.direction == "win" and s.kind == kind)
    assert step.target_mmr == expected
    assert step.games == (expected - 1000) // 10


@pytest.mark.parametrize("player_id,source", [("p", "personal"), ("other", "playlist")])
async def test_only_single_game_polls_count_and_shared_server_copies_do_not(
    tmp_path, player_id, source
):
    from slopbot.rank_estimates import estimate_rank_steps

    store = SubscriptionStore(tmp_path / "repeated.db")
    await store.initialize()

    def seed(c):
        for at in range(100, 108):
            for guild in (1, 2):
                c.execute(
                    "INSERT INTO rocket_league_match_history "
                    "(guild_id,discord_user_id,player_id,playlist,result,match_count,"
                    "observed_at,mmr_before,mmr_after) VALUES (?,2,'p',11,'win',1,?,1000,1010)",
                    (guild, at),
                )
        for at in range(200, 220):
            for result in ("win", "win" if at % 2 else "gap"):
                c.execute(
                    "INSERT INTO rocket_league_match_history "
                    "(guild_id,discord_user_id,player_id,playlist,result,match_count,"
                    "observed_at,mmr_before,mmr_after) VALUES (1,2,'p',11,?,1,?,1000,1030)",
                    (result, at),
                )
        c.execute(
            "INSERT INTO rocket_league_events "
            "(guild_id,discord_user_id,player_id,player_name,playlist,event_type,occurred_at,"
            "rank_before,rank_after,mmr_before,mmr_after) "
            "VALUES (1,2,'p','P',11,'rank_up',300,"
            "'Diamond II, Division II','Diamond II, Division III',1015,1025)"
        )
        c.commit()

    await store._run(seed)
    skill = RocketLeagueSkill("p", 1000, 14, 1, "Diamond II, Division II", playlist=11)
    result = await estimate_rank_steps(
        store, skill=skill, playlist=11, player_id=player_id, now=400
    )
    assert result is not None
    step = next(s for s in result.steps if s.direction == "win" and s.kind == "division")
    assert step.delta_source == source
    assert step.delta_samples == 8
    assert step.games == 2
