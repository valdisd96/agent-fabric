# Auto-deploy setup — one-time provisioning runbook

A managed project that wants the auto-deploy described in DESIGN.md
"Decision 15 — Deployment of managed projects" needs three pieces of host
state set up once. After that, every push to `main` auto-deploys via
`examples/github-actions/deploy.yml`.

This runbook is for the human operator. It does not need to be re-run on
each deploy.

## Prerequisites

- The project is already installed on a Linux VM and runs under a systemd
  unit (e.g. `teach-me-eng-bot.service`).
- `gh` CLI is installed and authenticated with the GitHub account that
  owns the project repo.
- The VM has internet access (the runner needs outbound HTTPS to GitHub).

## 1. Pick an install directory the runner can write to

The deploy workflow does `git reset --hard` inside the project's install
directory. The runner user (`github-runner` by default) needs to be able
to read+write it.

If the project currently lives under `/root/<project>` (root-owned), you
have two options:

- **Recommended**: relocate to `/srv/<project>`, owned by `github-runner`.
  ```bash
  sudo systemctl stop <project>.service
  sudo mv /root/<project> /srv/<project>
  sudo chown -R github-runner:github-runner /srv/<project>
  # Point the systemd unit at the new path. Edit:
  #   WorkingDirectory=/srv/<project>
  #   EnvironmentFile=/srv/<project>/.env
  #   ExecStart=/srv/<project>/.venv/bin/python <entrypoint>
  sudo systemctl daemon-reload
  sudo systemctl start <project>.service
  ```
- **Pragmatic**: keep the existing path and let the workflow run the git
  ops via `sudo -n`. The shipped `deploy.yml` already uses `sudo -n` for
  every host operation, so this works as long as `github-runner` has
  passwordless sudo (it does on this fabric VM).

Whichever you pick, set `INSTALL_DIR` in the workflow to match.

## 2. Register a repo-scoped self-hosted runner

The fabric VM may already host an organization-scoped runner (e.g. the
`Contable-Bot-ViVi` runner registered under `/home/github-runner/`). That
runner cannot serve a personal-account repo. Add a second runner scoped
to *this* repo.

```bash
# As root, in a fresh location:
sudo install -d -o github-runner -g github-runner /srv/runners/<project>
sudo -u github-runner -i bash <<'EOF'
  cd /srv/runners/<project>
  curl -fsSL -o runner.tar.gz \
    https://github.com/actions/runner/releases/download/v2.329.0/actions-runner-linux-x64-2.329.0.tar.gz
  tar xzf runner.tar.gz
EOF

# Get a registration token (one-time, expires in ~1h):
gh api -X POST repos/<owner>/<project>/actions/runners/registration-token --jq .token

# Configure (interactive — follow the prompts):
sudo -u github-runner -i bash -c "
  cd /srv/runners/<project>
  ./config.sh \
    --url https://github.com/<owner>/<project> \
    --token <TOKEN_FROM_ABOVE> \
    --name <project>-runner \
    --labels self-hosted,deploy-target,<project> \
    --work _work \
    --unattended
"

# Install as a systemd service so it starts on boot:
sudo /srv/runners/<project>/svc.sh install github-runner
sudo /srv/runners/<project>/svc.sh start
```

Verify with `gh api repos/<owner>/<project>/actions/runners` — the new
runner should appear with status `online` and the labels you set.

The label `deploy-target` is what the workflow's `runs-on:` matches on.
Keep it; add the project name as a second label so a future multi-project
deploy can route correctly.

## 3. Create the deploy state directory

The workflow writes the per-deploy manifest to
`/var/lib/<project>/deploy.json`. Create the directory once, owned by the
runner user:

```bash
sudo install -d -o github-runner -g github-runner -m 0755 /var/lib/<project>
```

## 4. (Optional) Connect the workflow to fabric

If fabric is running on the same VM and you want deploys recorded in its
SQLite + surfaced via Telegram, set two repo secrets:

```bash
gh secret set FABRIC_DEPLOY_URL -R <owner>/<project> --body "http://127.0.0.1:7878"
gh secret set FABRIC_TOKEN      -R <owner>/<project> --body "$(openssl rand -hex 32)"
```

The matching token must also be added to fabric's `/etc/fabric/env` once
the `/api/projects/<name>/deployments` endpoint lands (Phase: post-deploy
template). Until that endpoint exists, leave the secrets unset — the
workflow's notify steps detect the missing URL and skip silently. Auto-deploy
itself works in this bootstrap mode.

## 5. Drop the workflow file into the project repo

Copy `examples/github-actions/deploy.yml` from agent-fabric into the
project at `.github/workflows/deploy.yml`, fill in the five `CONFIGURE:`
values in the `env:` block at the top, commit, and merge to `main`. The
workflow does NOT trigger on the merge that introduces it (the workflow
file isn't yet on `main` from the runner's perspective at evaluation
time); the next merge after that is the first auto-deploy.

## Verifying the first deploy

1. Open a trivial PR (e.g., `README.md` typo). Merge it.
2. Watch `gh run watch -R <owner>/<project>` from your laptop.
3. On the VM: `journalctl -u <project>.service -f` — you should see the
   process exit + restart cleanly.
4. Confirm `/var/lib/<project>/deploy.json` has the new sha.

If the workflow fails mid-deploy, the broken version is still running on
the host (no auto-rollback). The next fix-PR redeploys and supersedes it.

## What this does NOT cover

- Zero-downtime deploys. The unit restarts; expect a 2–5s blip.
- Database migrations beyond what `scripts/migrate.sh` provides. The
  workflow runs that script if it exists; nothing more.
- Deploying multiple branches. Only `main` triggers; `workflow_dispatch`
  re-deploys whatever is at `main` on demand.
- Secret rotation. `FABRIC_TOKEN` and any project secrets in `.env` are
  managed out-of-band.
