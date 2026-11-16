from slopbot.rltracking import infer_activity_results


def test_single_match_uses_streak_direction() -> None:
    inferred = infer_activity_results(
        previous_matches=10,
        previous_win_streak=2,
        previous_mmr=1000,
        current_matches=11,
        current_win_streak=3,
        current_mmr=1010,
    )

    assert inferred.unknown_count == 0
    assert inferred.known_tail == ("win",)


def test_zero_streak_single_match_uses_mmr_direction() -> None:
    inferred = infer_activity_results(
        previous_matches=10,
        previous_win_streak=2,
        previous_mmr=1000,
        current_matches=11,
        current_win_streak=0,
        current_mmr=990,
    )

    assert inferred.unknown_count == 0
    assert inferred.known_tail == ("loss",)


def test_multiple_matches_preserve_only_provable_tail() -> None:
    inferred = infer_activity_results(
        previous_matches=10,
        previous_win_streak=4,
        previous_mmr=1000,
        current_matches=15,
        current_win_streak=3,
        current_mmr=1020,
    )

    assert inferred.unknown_count == 2
    assert inferred.known_tail == ("win", "win", "win")


def test_large_gap_is_not_expanded_or_guessed() -> None:
    inferred = infer_activity_results(
        previous_matches=10,
        previous_win_streak=1,
        previous_mmr=1000,
        current_matches=50,
        current_win_streak=8,
        current_mmr=1100,
    )

    assert inferred.unknown_count == 40
    assert inferred.known_tail == ()
