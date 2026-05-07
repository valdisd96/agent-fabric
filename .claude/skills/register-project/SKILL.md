---
name: register-project
description: End-to-end procedure for adding a brand-new managed project to a running agent-fabric — authoring `.fabric/config.yaml` from scratch, registering it, provisioning labels, syncing skills, and (optionally) wiring drift CI + auto-deploy. Invoke when the user asks to "add", "register", "onboard", "wire up", or "manage" a new project (anything other than teach-me-eng-bot, which has a copy-paste shortcut in the install skill). Project-internal; not rendered into managed projects by `fabric sync`.
version: 1.0.0
---

# register-project

Bring a brand-new repo under fabric management. This is the runbook for
project number ≥ 2 — for the very first project (teach-me-eng-bot
specifically) the install skill's Step 8 already handles it via
`cp examples/teach-me-eng-bot.config.yaml`. This skill is for any other
repo, where you have to author `.fabric/config.yaml` from the schema.

If the fabric service is **not yet running**, stop and point the user at
the `install` skill — this skill assumes a healthy `systemctl is-active
fabric` on the host. If they're trying to do both at once, finish the
install first (through the hand-off checklist), then come back here.

## What you'll end up with

```
/srv/projects/<repo>/                          # cloned managed repo (fabric:fabric)
/srv/projects/<repo>/.fabric/config.yaml       # ← the file you author in Step 2
/srv/projects/<repo>/.claude/skills/{plan-exec,test-writer,review-pr,
                                     clarify-issue,epic-decompose,
                                     qualify-issue,deploy-diagnose}/SKILL.md
                                               # ← rendered by `fabric sync`, committed upstream
/var/lib/fabric/projects.yaml                  # one new entry, name + path + repo
```

The fabric's next 60-second tick will pick up any pre-existing
`state:needs-planning` issues in the new repo and start dispatching.

## Prerequisites — confirm before starting

- The fabric service is running (`systemctl is-active fabric` → `active`)
  and at least one project (e.g. teach-me-eng-bot) is already healthy.
- The new repo exists on GitHub and the fabric's `gh` auth (logged in
  as the `fabric` system user) has **read + write** access to it. For
  fine-grained PATs that means `Contents: Read and write` granted to
  the specific repo — read-only is enough to register but `git push`
  fails when the dispatcher tries to open a PR.
- You know what trivial first issue you'll file as a smoke test
  (Step 9) — a one-line README/docs change is ideal.
- The repo has at least one committed file on `main` (the dispatcher
  rebases against `origin/main` every cycle; an empty repo can't be
  rebased onto).

If anything is missing, pause and ask — don't fabricate a config or
guess at what `area_labels` belong on a project you haven't read.

## Step 1 — read the project before writing the config

Before authoring `.fabric/config.yaml`, scan the repo so the values you
choose are grounded in reality, not fabricated. At minimum, look at:

- **Top-level layout** — what languages, what entry points, what's the
  rough module split. Drives `modules:` (Step 2).
- **Build/test commands** — `pyproject.toml`, `package.json`, `Makefile`,
  `scripts/`, README's "how to test". Drives `build.test_cmd` and
  `build.setup_cmd`.
- **Dangerous spots** — auth glue, payment code, migration scripts,
  vendored binaries, `.github/workflows/`. Drives `safety.blocked_paths`.
- **Schema migrations / DB layer** — anything resembling SQL DDL. If
  the project has destructive operations that must never be auto-applied,
  drives `safety.destructive_db_patterns`.
- **Existing labels on issues/PRs** — if the project already has its own
  vocabulary, decide whether to keep them (and if so add `area:*` only)
  or replace them; `setup-labels --check` will show the diff.

You don't need to be exhaustive — modules and safety can be tightened
later by editing the YAML and re-running `setup-labels`/`sync`. But
shipping a vague config wastes the agents' attention budget on every
dispatch.

## Step 2 — author `.fabric/config.yaml`

The fastest way is to copy `examples/teach-me-eng-bot.config.yaml` and
edit field by field. The schema is **strict** (`extra="forbid"`) — typos
in keys fail loudly with a field path on `register`. Every field below
is required unless marked optional.

```yaml
project:
  name: <slug>                  # e.g. "my-new-bot" — used as the
                                # registry key, label suffix, and
                                # /srv/projects/<this>. Must be unique.
  repo: <owner>/<repo>          # GitHub owner/repo. Validated against
                                # ^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$.
  trusted_authors:              # GitHub usernames the agents treat as
    - <gh-handle>               # authoritative when reading issue/PR
                                # comments. Usually just the project owner.

build:
  setup_cmd: "source .venv/bin/activate"   # optional; runs before tests
  test_cmd:  "python -m pytest -q"         # required; the agents run
                                           # this before commits
  lint_cmd:  null                          # optional

modules:                                   # informs the agents about
  - path: <relative/path.py>               # the project structure.
    role: "<one-line role>"                # 5-15 lines is plenty.
  # ...

safety:
  blocked_paths:                           # globs the agents will not
    - .github/workflows/**                 # touch under any circumstance
    - infra/terraform/**
  destructive_db_patterns:                 # plain strings or regex
    - "DROP TABLE"                         # fragments — the agent
    - "DROP COLUMN"                        # refuses to commit a diff
    - "ALTER COLUMN .* DROP"               # that matches any of these
  notes: |
    Any free-form caveats. Load-bearing invariants the agents must
    not break (e.g. "FSRS columns are load-bearing — do not touch").

labels:
  state_prefix: "state:"                   # leave as-is unless you have
  type_prefix:  "type:"                    # a strong reason — fabric's
  area_prefix:  "area:"                    # default labels assume these
  area_labels:                             # drives `area:<x>` labels
    - <area-slug>                          # provisioned by setup-labels
    # ...

pipeline:
  cycle_limit: 5                           # max plan-exec→test-writer
                                           # round-trips per issue before
                                           # the issue auto-blocks
  retry_count: 3                           # transient-failure retries
                                           # per dispatch
  retry_backoff_seconds: [60, 300, 900]    # backoff schedule (len ==
                                           # retry_count typically)
  dispatch_cap: 30                         # rolling-window cap; see
  dispatch_window_hours: 5                 # DESIGN.md "Decision 17".
                                           # Defaults match Anthropic's
                                           # 5h subscription window.

fabric_version: "0.2.0"                    # MUST match a release the
                                           # running fabric supports.
                                           # Major mismatch → sync fails.
```

**Picking `dispatch_cap` and `dispatch_window_hours`.** If this is the
*only* project, leaving the defaults (30 / 5h) gives full headroom under
the Anthropic Pro window. If you're adding a second project that shares
the same fabric, decide how to *split* the budget — fabric tracks the
window per-project, but the underlying API quota is shared. Halving
each project's `dispatch_cap` is the conservative call.

**Picking `cycle_limit`.** 5 is a reasonable default. Raise it for
projects where plan-exec genuinely needs multiple passes (large
refactors, ill-specified issues); lower it (3) for projects where
runaway looping is more concerning than under-progress.

**Picking `fabric_version`.** Read the running fabric's version:
`sudo -u fabric /srv/agent-fabric/.venv/bin/fabric --version` — wait,
`fabric` doesn't have `--version` yet. Use `pip show agent-fabric` from
inside the venv, or `grep ^version /srv/agent-fabric/pyproject.toml`.
Pin to that exact value. If the running fabric is `0.2.x`, both
`"0.2.0"` and `"0.2.5"` work; `"0.3.0"` would be future-incompatible.

## Step 3 — clone the repo under `/srv/projects` as the fabric user

The path must be `/srv/projects/<repo>` because that's where
`install-systemd.sh` set up the writable directory and where the
running fabric expects clones. The `repo` directory name should match
the GitHub repo name, not necessarily `project.name` (they often
match anyway).

```bash
sudo -u fabric -H bash <<'EOF'
set -euo pipefail
export FABRIC_HOME=/var/lib/fabric        # ← MUST match /etc/fabric/env
cd /srv/projects
git clone https://github.com/<owner>/<repo>
EOF
```

If `git clone` errors on auth, the fabric's `gh` PAT doesn't have read
access to this repo. Re-grant via `sudo -u fabric -H gh auth refresh
-h github.com -s repo` or rotate the PAT.

## Step 4 — drop the authored config into the clone

Author the YAML on your laptop (or anywhere convenient), copy it to
the VPS, then place it under `.fabric/config.yaml` in the new clone.
The file must be readable by `fabric:fabric`.

```bash
# From your laptop:
scp my-new-bot.config.yaml vps:/tmp/

# On the VPS:
sudo -u fabric -H bash <<'EOF'
set -euo pipefail
export FABRIC_HOME=/var/lib/fabric
PROJECT=/srv/projects/<repo>
mkdir -p "$PROJECT/.fabric"
cp /tmp/my-new-bot.config.yaml "$PROJECT/.fabric/config.yaml"
EOF
```

Don't put the YAML in an `examples/` subdirectory of agent-fabric — it
belongs *inside the managed project*, alongside the project's own code.
Eventually you'll commit it upstream so the project repo is the
authoritative source.

## Step 5 — register, provision labels, sync

```bash
sudo -u fabric -H bash <<'EOF'
set -euo pipefail
export FABRIC_HOME=/var/lib/fabric
PROJECT=/srv/projects/<repo>
NAME=<project-name>                         # the project.name from your YAML
F=/srv/agent-fabric/.venv/bin/fabric

"$F" register "$PROJECT"                    # validates schema, prints registry path
"$F" setup-labels "$NAME" --check           # show the label diff (dry run)
"$F" setup-labels "$NAME"                   # apply: creates state:*, priority:*,
                                            # type:*, area:* labels in the GH repo
"$F" sync "$NAME"                           # renders the 7 skills into
                                            # $PROJECT/.claude/skills/
EOF
```

What can fail and how to read it:

| Stage | Symptom | Fix |
|---|---|---|
| `register` | `<path>: ...config.yaml: <field>: <error>` | Schema violation. Read the field path; fix the YAML. `extra="forbid"`, so a typo in a key name fails here. |
| `register` | `warning: /etc/fabric/env sets FABRIC_HOME=...` | You forgot `export FABRIC_HOME=...` in the heredoc. The CLI wrote to `~/.fabric/projects.yaml` which the service doesn't read. Re-run with the export. |
| `setup-labels` | `setup-labels: project '<name>' not registered` | `register` didn't take, or the `name` you passed doesn't match `project.name` in the YAML. |
| `setup-labels` | `gh: ... HTTP 403` | PAT missing repo write or `repo` scope on this specific repo. |
| `sync` | `template not found: <name>` | Fabric version mismatch — `fabric_version` in YAML pins a major the running fabric doesn't ship. |
| `sync` | drift on existing project | Someone hand-edited `.claude/skills/` in the project. Run with `--check` to see the diff; if intentional, commit it upstream so the next sync is clean. |

## Step 6 — commit the rendered skills upstream

After `sync`, the project's working tree at `/srv/projects/<repo>` has
seven new (uncommitted) skill files. Two ways to land them on GitHub:

**Recommended — from the operator's local clone, not the VPS.** The VPS
clone is the dispatcher's working copy and gets rebased against
`origin/main` every cycle. Use it as a *reference* for what the synced
files look like, not as the source of the commit.

```bash
# On your laptop, in a fresh clone of the project:
cp /srv/agent-fabric/examples/teach-me-eng-bot.config.yaml \
   .fabric/config.yaml                    # OR: scp the YAML you authored
mkdir -p .fabric

# Locally install agent-fabric the same way the VPS does, then:
fabric register .
fabric sync $(yq .project.name .fabric/config.yaml)

git checkout -b chore/wire-fabric
git add .fabric/ .claude/skills/
git commit -m "chore: wire agent-fabric (skills + config)"
git push -u origin HEAD
gh pr create --base main --title "chore: wire agent-fabric" \
             --body "Adds .fabric/config.yaml and renders the 7 fabric-managed skills."
```

After the PR merges, on the VPS:

```bash
sudo -u fabric -H bash -c '
  cd /srv/projects/<repo> && git fetch && git reset --hard origin/main
'
```

The VPS clone now matches what the dispatcher will rebase onto.

**Pragmatic — commit + push directly from the VPS clone.** Works if
the `fabric` user's `gh` PAT has write access; just be aware the VPS
clone *is* the dispatcher's working copy, so don't leave dirty trees
or partial branches behind.

## Step 7 — drop in the drift-CI workflow (recommended)

Without this, anyone can hand-edit a file under `.claude/skills/` and
the dispatcher will keep re-rendering it on every sync — annoying noise.
With it, every PR runs `fabric sync . --check` and fails if the
committed skills don't match what the pinned fabric version would
render.

```bash
# In the operator's local clone:
mkdir -p .github/workflows
cp /path/to/agent-fabric/examples/github-actions/fabric-sync-check.yml \
   .github/workflows/fabric-sync-check.yml
# Edit the file: replace REPLACE_WITH_AGENT_FABRIC_COMMIT_SHA with the
# agent-fabric commit you want to pin. Match the running fabric or pin
# slightly newer if you're staging an upgrade.
git add .github/workflows/fabric-sync-check.yml
git commit -m "chore: add fabric drift CI"
```

The PR with the workflow can be the same PR as Step 6, or a follow-up.

## Step 8 — (optional) wire auto-deploy

If the project runs as a systemd service on the same VPS and you want
push-to-main → auto-redeploy → diagnostic-issue-on-failure, follow
[`examples/runbooks/deploy-setup.md`](../../../examples/runbooks/deploy-setup.md)
end-to-end. It's a one-time host setup (self-hosted runner +
`/var/lib/<project>/` + workflow file). Not required to use the fabric;
plenty of managed projects deploy by other means or not at all.

The auto-deploy story integrates with `deploy-diagnose` (the 7th
fabric-managed skill) — see DESIGN.md "Decision 15".

## Step 9 — smoke test

Open a trivial issue in the new repo:

- Title: `smoke: <one-line trivial change>` — e.g. "fix typo in README".
- Body: a one-sentence diff description.
- Labels: `state:needs-planning`, `priority:low`, `type:docs` (or
  whatever fits), and exactly one of the project's `area:*` labels.

Then watch progression in three places, same as the install smoke:

```bash
sudo journalctl -u fabric -f
sudo -u fabric /srv/agent-fabric/.venv/bin/fabric logs <project> <issue#> --follow
# Telegram: /queue, /status
```

Within ~60 seconds the scheduler should pick up the issue. Expected
state machine (no different from teach-me-eng-bot):

```
state:needs-planning → state:in-progress → state:tests-pending → state:in-review
                                                                      → TG buttons
                                                                      → user merges
```

If the issue sits at `state:needs-planning` for >2 minutes:

- Confirm it appears in `fabric status` (run as fabric, with
  `FABRIC_HOME` exported). If not, the registry didn't take.
- Confirm none of the other paused gates are set: `fabric status`
  also shows the global `paused` flag.
- Tail `journalctl -u fabric` for "no actionable issues" — the
  scheduler logs each tick. If it never mentions the new project,
  something's wrong with how it was registered.

## Hand-off checklist

Before declaring the new project onboarded:

- [ ] `.fabric/config.yaml` validates (`fabric register` succeeded)
- [ ] `setup-labels --check` exits 0 (labels match the canonical set)
- [ ] `sync --check` exits 0 (no drift between templates and committed skills)
- [ ] Rendered `.claude/skills/` is committed upstream on `main`
- [ ] `fabric status` lists the project with a non-zero issue count
      *or* "0 issue(s) tracked" only because nothing is labeled yet
- [ ] (If wired) drift CI workflow is on `main` and the next PR
      runs it green
- [ ] Smoke issue cycled through `needs-planning → in-progress → tests-pending → in-review`
      and a PR opened

If any item fails, debug it before filing real work into the queue —
a half-onboarded project will silently misbehave on the next dispatch.

## Common mistakes

- **Authoring the YAML with comments shaped from teach-me-eng-bot.**
  The teach-me-eng-bot example mentions FSRS columns and python-telegram-bot
  details that have nothing to do with the new project. Strip every
  reference and replace with the new project's actual invariants.
- **Forgetting to commit `.fabric/config.yaml` itself.** The fabric
  works without the upstream commit (it reads the local clone's
  `.fabric/config.yaml`), but every fresh clone of the repo would have
  to re-author it. Always commit upstream alongside the rendered
  skills.
- **Pinning `fabric_version` looser than necessary.** Pin to the exact
  patch the host runs. `"0.2.0"` is honest; `">=0.2"` is not a thing
  the schema accepts anyway, but operators sometimes try.
- **Skipping `setup-labels` because "GitHub already has some labels".**
  The dispatcher reads `state:*` labels; without them, every issue
  looks like nothing-to-do. Run `setup-labels --check` first to see
  exactly what's missing.
- **Listing a project name with whitespace or capitals.** `name`
  becomes a path segment under `/srv/projects/` and a label suffix.
  Lowercase kebab-case only.

## What this skill deliberately does not cover

- **Authoring overlay skills** (`<project>/.fabric/skills/<name>/SKILL.md.j2`).
  Most projects don't need them — the seven defaults are deliberately
  generic. If a default skill genuinely doesn't fit (e.g. the project
  has a non-pytest test runner that `test-writer.md.j2` can't be
  parameterized into), write a full skill overlay; see DESIGN.md
  "Override grain". Don't do this preemptively.
- **GitHub App auth** — fabric currently uses a `gh` PAT. Switching to
  GitHub App auth is Phase 3 and won't change this skill.
- **Removing a project.** Delete the entry from
  `/var/lib/fabric/projects.yaml`, `rm -rf /srv/projects/<repo>`, and
  optionally drop the rendered skills upstream. There's no
  `fabric unregister` command yet.
