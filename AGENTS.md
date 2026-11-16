# Slop Bot working rules

- The website is a first-class Slopbot interface. Every added or changed feature must have
  corresponding web support, server-side authorization, and regression coverage in the same
  change. Reuse shared feature handlers; do not let Discord and web behavior drift. Run the web
  command/interactive-component coverage checks alongside existing checks before shipping.

- At the start of every task, inspect `git status`, fetch `origin`, and compare the checkout with
  the freshly fetched `origin/main` before auditing or editing. Do not assume a local tracking ref
  is current. Start new work from the latest main, or bring an existing feature branch up to date.
- On a clean main checkout, use `git merge --ff-only origin/main`. Preserve dirty, untracked, and
  unrelated work before any checkout or integration. If main diverged, inspect both histories and
  integrate safely; never discard commits, use a destructive reset, or force-push to make it match.
  If fetching or safe integration is blocked, state that explicitly instead of claiming it is synced.
- Fetch again before integrating or pushing. Reconcile new remote commits without overwriting
  others' work, rerun checks affected by the integration, and push only when the task authorizes it.
  This is a per-task workflow rule, not a background pull job.
- Prefer demonstrated bug fixes and small changes that preserve existing commands, configuration,
  account history, and notification behavior. Run relevant regression tests and the repository's
  lint, formatting, type, and service checks before shipping.
