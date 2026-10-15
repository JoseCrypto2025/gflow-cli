# Git Baseline

Recorded: 2026-10-15

## Canonical repository

- Local checkout: `D:\Gflow-CLI`
- Fork / origin: `https://github.com/JoseCrypto2025/gflow-cli`
- Upstream: `https://github.com/ffroliva/gflow-cli`
- Both fetches completed after clone; origin and upstream remotes are configured.

## Baseline and branch

- Upstream default branch: `develop` (verified from advertised HEAD and `upstream/HEAD`).
- Upstream baseline commit: `38ce8ac8f299e19c9d35e1def30ab7339ad64cdc`.
- New branch: `automation-v2`, created directly from that upstream commit.
- Starting commit SHA: `38ce8ac8f299e19c9d35e1def30ab7339ad64cdc`.
- Published to origin; local branch tracks `origin/automation-v2`.

## Preserved fork state

- Fork default branch: `develop`, at `342f39df5fd7835db59373367bf66ec85de16566` at clone/fetch time.
- Existing remote branch `origin/ai-project-secretary-integration` was observed at `a130b04f`; it was not checked out, merged, rebased, or copied.
- The initial clone had a clean working tree. The local `develop` branch and all fetched remote branches/tags remain intact.
- The repository inventory after both fetches contained 2 local branches, 2 origin tracking branches, 45 upstream tracking branches, and 108 tags (HEAD aliases excluded from branch counts).

## Access notes

- The first local Git push could not prompt for a password. The connected GitHub account reported admin permission on the fork, so the branch was created through the GitHub integration at the same verified SHA and fetched back locally. No repository history was rewritten.