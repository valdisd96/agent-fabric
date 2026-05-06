# #44 — `setup-labels` vocabulary migration

**Status:** open · filed 2026-05-05 · no PR yet
**GitHub:** https://github.com/valdisd96/agent-fabric/issues/44

## What

`fabric setup-labels` is purely additive today — it creates missing canonical labels and updates colors/descriptions but **never removes obsolete labels** and **can't accommodate projects with their own pre-existing `type:*` vocabulary**.

## Why it matters

Hit while bringing the new epic flow live on `teach-me-eng-bot`. The project already had `type:feat / type:fix / type:chore` (its own vocabulary). Running `setup-labels` would have created the canonical `type:feature / type:bug / type:test / type:docs / type:epic` alongside, leaving ~9 `type:*` labels coexisting and confusing both humans and agents (the templates pick canonical names; the user picks their own). Same problem will hit every new managed project that didn't start with the canonical vocab.

Separately, after the rename in #43 (`state:epic` → `state:needs-decompose`), legacy labels can linger in repos and need manual cleanup.

## Direction sketch (to be designed when picked up)

- `.fabric/config.yaml` could declare `labels.type_labels: [feat, fix, chore]` to override the canonical set per-project. Today only `area_labels` is parameterized.
- `LabelSpec` could carry `former_names: list[str]` so `setup-labels` can do color-preserving renames after a fabric vocab change (`gh label edit <old> --name <new>`).
- Optional `--prune-obsolete` flag to surface labels not in the current canonical *and* not in the project override, prompt to delete.
- Re-think whether agents should be vocabulary-tolerant: e.g., `qualify-issue` could read the project's pinned type set rather than hard-coding `type:feature`.

## Workaround until then

For each new managed project: skip `setup-labels`, create just the needed `state:*` + `type:epic` + `priority:*` labels manually via `gh label create`. That's what we did for `teach-me-eng-bot` post-#43.

## Out of scope

- The existing `area:*` per-project parameterization is fine; this issue is about types and migrations.
