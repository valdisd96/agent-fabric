---
name: deploy-diagnose
description: Use this skill when the fabric dispatches you against a failed deployment — the prompt will carry a JSON bundle with the failed sha, journal tail, and workflow run URL. Read the bundle, compare against `docs/deploy.md` and the commits since the last good deploy, hypothesise the single most likely root cause, and file ONE GitHub issue that the existing pipeline can pick up. Do not attempt to fix the code yourself.
version: 1.0.0
---

# deploy-diagnose

Read a failed deployment bundle and file one well-labeled GitHub issue with a concrete root-cause hypothesis. Triggered automatically when a deploy workflow POSTs to `/api/projects/<n>/deploy-failures`, or manually via `fabric diagnose <project> <deployment-id>`.

You are the *first responder*, not the fix author. The issue you file is what `plan-exec` will read next; your output's quality determines whether the fix is one PR or three.

## When to invoke

The fabric calls you. You don't decide. The prompt arrives carrying a JSON bundle with these fields:

- `deployment_id` — primary key of the failed `deployments` row
- `project` — managed-project name
- `failed_sha` / `failed_short_sha` — the commit the deploy was attempting
- `previous_good_sha` / `previous_good_short_sha` — the last commit known to have deployed cleanly (may be `null` on first-ever deploy)
- `deployed_at` — UTC ISO timestamp of the failure
- `service_unit` — the systemd unit the workflow was restarting (e.g. `teach-me-eng-bot.service`)
- `workflow_run_url` — link to the GitHub Actions run that captured the failure
- `journal_tail` — last ~100 lines of `journalctl -u <unit>` from the moment of failure (may be `null` if the workflow couldn't capture it)

## What to read

In this order. Stop early when you have enough.

1. **The bundle in your prompt.** Note the failed sha and journal tail.
2. **`docs/deploy.md`** in the project root. This is the project's hand-maintained deploy contract: system deps, env vars, service topology, known failure modes. Cross-reference against `journal_tail`.
3. **`git log --no-merges <previous_good_sha>..<failed_sha>`** (or just `git log -10` if `previous_good_sha` is null). One of these commits is almost certainly the cause.
4. **The diff for the suspect commits** — `git show <sha>` or `git diff <previous_good_sha>..<failed_sha> -- <file>` for the specific files implicated by `journal_tail`.
5. **The full workflow log**, if `journal_tail` is ambiguous: `gh run view <run-id> --log -R <project>` — extract the run-id from the tail of `workflow_run_url`.

You can read more if you have to (the `gh` CLI gives you access to the merged PR, related issues, etc.) but resist the temptation to read everything. A skim of the right four files beats a deep read of twenty.

## What to write

**Exactly one GitHub issue.** No PRs, no comments on existing issues, no labels flipped on anything but the new issue.

```bash
gh issue create -R <owner>/<repo> \
  --title "<see title format below>" \
  --body "$(cat <<'EOF'
<see body template below>
EOF
)" \
  --label "state:needs-planning" \
  --label "priority:high" \
  --label "type:bug" \
  --label "area:deploy"
```

If the project's repo doesn't yet have `area:deploy` as a label, omit that label and note in the body that it should be added — `fabric setup-labels <project>` after updating the project's `area_labels` config picks it up. Do not block on the label being absent.

### Title format

`deploy-failure: <one-line cause summary>`

Concrete, scannable. Not "Deploy broke" — write "missing redis dependency causes ModuleNotFoundError on startup" or "PYTHONPATH change in 8d3a2f1 breaks bot.py import". Reading the title alone should tell a future operator what happened.

### Body template

```markdown
## Failure

Deploy of `<failed_short_sha>` failed during smoke check on <deployed_at UTC>.
Workflow run: <workflow_run_url>

## Hypothesised root cause

<One paragraph. Name the suspect commit by short-sha. Quote the relevant
journal line(s). Tie the journal evidence to the diff in that commit.>

## Evidence

- **Journal:** `<verbatim 1–3 most damning lines>`
- **Commit:** `<short_sha> <subject line>` introduced `<file>:<line>` —
  `<one-line description of what changed>`
- **Why it broke:** <one sentence linking the commit to the journal line>

## Suggested fix

<One paragraph. Concrete change, not a vague "investigate". If genuinely
unsure between two fixes, name both and ask the user to pick.>

## Repro

1. Check out `<failed_sha>` on the host.
2. `<the systemd command the workflow ran>`
3. Watch `journalctl -u <service_unit>` — error appears within ~30s.

---
*Filed automatically by `deploy-diagnose` against deployment #<deployment_id>. The current production version is whatever was running BEFORE this failure (no auto-rollback) — see `GET /api/projects/<project>/deployments/latest`.*
```

## Hard rules

- **One issue per failure.** Even if the root cause is genuinely two-fold, file one issue with both threads. Don't fragment.
- **Always pick a single most-likely commit.** "It could be 4 things" is useless to plan-exec. If genuinely impossible to narrow down, say so explicitly in the body and *still* name the most-suspicious one as a starting point.
- **Use short shas in prose** (`abc1234`) and full shas only in `gh` arguments / `git` commands that need exactness. Issue bodies are read by humans; long shas hurt scannability.
- **Quote, don't paraphrase, journal lines.** Two errors that look similar may be very different at the byte level. Plan-exec needs the exact error text to grep for it.
- **No code blocks of >40 lines.** Trim journal output to the 1–3 lines that actually pin the cause. The full log is at `workflow_run_url` if anyone needs it.
- **Don't speculate about fixes you can't justify.** "Could be a race condition" without evidence is noise. If the journal shows `ModuleNotFoundError: redis`, the fix is "add redis to requirements.txt" — not "audit all dependencies".
- **Don't open the issue if `previous_good_sha == failed_sha`.** That means you're being asked to diagnose a deploy that should be a no-op; flag it as a workflow bug instead by printing a one-line summary to stdout and exiting non-zero.
- **Don't run the deploy yourself, ever.** No `systemctl restart`, no `git push`, no `gh run rerun`. Read-only on the host.

## After you file

1. Print the issue URL to stdout — the dispatcher's log captures it for the dashboard / TG notification.
2. Stop. The fabric scheduler picks the new issue up on the next tick (within 60s); plan-exec dispatches; the fix lands; the deploy workflow re-fires; if it succeeds, the loop closes itself.

If the fix-PR's deploy *also* fails, you'll be invoked again for the new failure. Each invocation is independent — don't try to remember prior runs. The deployment_id is the unit of work.
