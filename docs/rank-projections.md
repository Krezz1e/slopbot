# Rank projection notes

## What the estimates mean

The division/rank up and down counts are conditional short-term estimates assuming consecutive
wins or losses. They do not predict when a player will reach a rank, nor the probability of doing
so. The game service remains authoritative for the current rank.

Epic's [matchmaking FAQ](https://www.epicgames.com/help/c-202300000001622/c-202300000001682/a202300000014019?lang=es-MX)
explains that match difficulty affects MMR gained or lost and that there is no fixed game count
for ranking up. The [June 2024 competitive rank update](https://www.rocketleague.com/news/changes-to-rocket-leagues-competitive-rank-system-for-better-matches)
also describes placement adjustments for stale playlists based on other competitive playlists.
Those adjustments are not a general model of future improvement. Keep ordinary estimates
playlist-specific and suppress them during recognized placements.

## Changes in this audit

- Preserve separate single-game observations with the same before/after MMR at different times.
  Copies of the same observation across servers still collapse. Previously, repeated games could
  fail the personal-evidence minimum or distort the median.
- Prefer a direct observed crossing over a stored reverse crossing, including whole-rank edges.
  Reverse crossings remain fallback evidence; they should not override the requested direction.
- Expose the same shared estimates through authorized current/most-recent server-session previews.
  Website estimates are collapsed initially, with targets and evidence available on expansion.

These fix demonstrated evidence-handling problems. No out-of-sample accuracy gain is claimed.

## Next short-term improvement to validate

Save prediction snapshots and evaluate them against later clean single-game observations, using
only evidence available at prediction time. Compare median absolute game-count error separately
for promotions/demotions, division/rank changes, playlists, and evidence source. Evaluate
consecutive-result runs separately from mixed-result sessions; a loss changes the premise of a
win-count estimate. Exclude placements, season resets, and gaps in observation from clean-run
scoring. Avoid treating copies from multiple servers as independent validation examples.

Then compare recent personal deltas against the current 30-day median, and direction-specific
boundaries against reverse or projected fallbacks. Introduce empirically calibrated ranges only
if held-out results support them. A range derived only from MMR-delta variation would omit
boundary uncertainty and should not be called a confidence interval.

Current limitations include unknown opponents, fallback boundaries inferred from nearby ranks,
and aggregate boundaries that can retain older influence even when refreshed recently. The
usual-results fallback also summarizes completed visits to a rank step, which can contain mixed
results; it is a rough fallback rather than a calibrated consecutive-run model.

## Longer-term idea, deferred

After collecting enough independent sessions, a playlist-specific model could estimate a range
of MMR after a chosen number of games using recent result rates and gain/loss distributions.
Validate it against a no-change baseline and account for uncertainty in the result rate. Predict
games first; converting to calendar dates adds another assumption about playing frequency.
Do not ship a long-term rank promise based on extrapolating a short winning streak.
