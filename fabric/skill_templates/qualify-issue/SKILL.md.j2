---
name: qualify-issue
description: Triage stage for freshly-filed open issues that have no `state:*` label. Reads the body and routes the issue into one of four buckets — `state:draft` (ignored), `type:epic` + `state:needs-decompose` (decompose), `state:clarification-needed` (ask the human), or `state:needs-planning` (ready for plan-exec) — by setting labels and, where appropriate, a single short comment. Read-and-label only; never edits code, never opens PRs, never touches more than one issue per dispatch.
version: 1.1.0
---

# qualify-issue

The first thing the fabric does to any new issue. One dispatch = one classification = one terminal `state:*` label set on the issue.

## Hard boundaries

You MAY:
- `gh issue view`, `gh issue list` (read-only on other issues for cross-reference resolution).
- `gh issue comment`, `gh issue edit` (labels) **on the issue you were dispatched on**.
- Read code with the `Read` / `Grep` tools to ground a scope estimate.

You MUST NOT:
- Edit code, create branches, commit, push, or open PRs. Triage is read-and-label only.
- Touch any issue other than the one you were dispatched on.
- Apply more than one `state:*` label.
- Skip applying a state label. Every dispatch must end with the issue holding exactly one `state:*` label so the next tick knows what to do.
- Apply `priority:*` or `type:*` you cannot defend from the body. When in doubt, leave them off — the next stage or the human will tag them.

## Inputs

You're dispatched on an open issue with **no** `state:*` label on GitHub. The fabric tags it `state:unqualified` internally — that label is not on GitHub, you don't need to remove anything before applying the real one.

## The decision tree

Walk top-down. **First match wins** — once a branch fires, set its labels and stop.

### Branch 1 — Draft / WIP

Match if **any**:
- The body or title contains "WIP", "draft", "not ready", "do not work on", "ignore for now", "[WIP]", "[draft]", or equivalent.
- The body explicitly says the author is still drafting / thinking out loud.

```bash
gh issue edit <N> --add-label "state:draft"
gh issue comment <N> --body "Marked as \`state:draft\` — agents will skip this issue. Remove the label when it's ready to work."
```

Stop.

### Branch 2 — Vague / unanswerable

Match if **all** of these hold even after re-reading the body and skimming related code:
- You cannot state in one sentence what the change is supposed to *do*.
- Two reasonable interpretations of the body would lead to different module changes.
- The issue contradicts itself, or asks for two unrelated things in the same body.

If yes, ask **one focused question** and park.

```bash
gh issue comment <N> --body "$(cat <<'EOF'
<!-- agent-qualify v1 -->
**Quick triage question before I queue this for planning:**

<the precise question — one paragraph, one decision>

Context: <one sentence — the load-bearing ambiguity, in your own words>.

Reply on the issue or via Telegram. Once you answer, remove `state:clarification-needed` and the next tick will re-triage.
EOF
)"
gh issue edit <N> --add-label "state:clarification-needed"
```

Stop. Do not also set `priority:*` or `type:*` — they may change after the answer.

The bar for asking is high. **You are not the planner.** Plan-exec will ask its own clarification later if needed. Ask only when the issue body is so under-specified that *any* attempt at a plan would be guesswork.

### Branch 3 — Epic

Match if **any** of these are clearly true from the body:
- The author explicitly calls it an "epic", "big feature", "multi-step initiative", or asks to "split into smaller issues".
- Implementation would obviously require **3+ separate PRs** to ship safely (e.g. new module + new schema + new UI + migration; multi-stage rollout with feature flags).
- The body lists 4+ acceptance criteria across **different concerns** (not 4 sub-checks of one thing — the test for "different concerns" is whether each criterion lives in a different module).

Most issues are **not epics.** A single feature, a bug fix, a refactor, a docs update, even a moderate cross-module change — these are plan-ready, not epics. Bias toward Branch 4. Reach for "epic" only when splitting it would be obviously wrong to do in a single PR.

```bash
gh issue edit <N> --add-label "type:epic" --add-label "state:needs-decompose"
gh issue comment <N> --body "Classified as \`type:epic\` + \`state:needs-decompose\` — \`epic-decompose\` will pick this up to start the Q&A and propose a child-issue list."
```

Stop. `type:epic` is the permanent marker on the parent (separates "what kind of issue" from "where in the pipeline"); `state:needs-decompose` is the transient routing state the scheduler keys on. Don't also set `type:feature` — `type:epic` covers it.

### Branch 4 — Plan-ready (default)

This is the **default branch** if 1–3 didn't fire. Use it whenever the issue is a single-deliverable change with enough detail for plan-exec to draft a plan, even if some details are fuzzy.

```bash
gh issue edit <N> --add-label "state:needs-planning"
# Add priority + type only if you can defend the choice from the body.
# When unsure, leave them off — plan-exec or the human will set them.
```

No comment is required for this branch. Quietness on the plan-ready path keeps the issue thread clean for the actual planning work.

#### Inferring `priority:*` (optional)

| Body signal | Label |
|---|---|
| "ASAP", "urgent", security risk, broken feature blocking other work | `priority:high` |
| Quality-of-life, tech debt, "nice to have", small UX, docs | `priority:low` |
| Anything else | `priority:medium` (or leave unset) |

Don't infer `priority:high` from author tone alone — look for a concrete reason.

#### Inferring `type:*` (optional)

| Body signal | Label |
|---|---|
| "fix", "broken", "doesn't work", reproducible defect | `type:bug` |
| "add", "support for", new capability | `type:feature` |
| "rename", "move", "clean up", "extract" with no behavior change | `type:refactor` |
| README, docstring, /help text, `*.md` | `type:docs` |
| Test coverage, fixtures | `type:test` |

#### Inferring `area:*` (optional)

If the issue body or title clearly references one of the project's `area:*` labels (each project's `.fabric/config.yaml` lists the valid set), apply it. Cross-cutting issues may take 2+ area labels. Skip if uncertain.

## Worked examples

These are sketches based on real issue shapes — actual classification will vary with the full body.

**Title: "Rewrite README. Leave only information about this bot's functionality"**
→ Branch 4. `state:needs-planning` + `priority:low` + `type:docs`. Single deliverable, scope is clear.

**Title: "Need ability to upload words by batches"** (body: use case, mentions import/export as a stretch)
→ Branch 4. `state:needs-planning` + `type:feature`. Borderline epic? No — one user-facing capability with one input format. Plan-exec can scope import/export as a follow-up if needed.

**Title: "Update /help command descriptions"** (body: "we just completed #49 but /help wasn't updated; please fix")
→ Branch 4. `state:needs-planning` + `priority:medium` + `type:docs`. Cross-references another issue but is itself a single small change.

**Title: "Potential security risk"** (body: notes that `ALLOWED_USER_IDS` is empty in some tested branch, suggests this might let anyone use the bot)
→ Branch 4. `state:needs-planning` + `priority:high` + `type:bug`. A defect with a concrete reproducer and a real consequence.

**Title: "Migrate auth + storage + UI to multi-tenant"** (body: lists 6 ACs spanning DB schema, OAuth, frontend, new admin pages, billing, docs)
→ Branch 3. `type:epic` + `state:needs-decompose`. 3+ PRs, different concerns. epic-decompose takes it from here.

**Title: "Refactor"** (body: empty)
→ Branch 2. Ask: "What part of the codebase, and what's wrong with the current shape that motivates the refactor?" Cannot reasonably plan from this.

**Title: "[WIP] Thinking about a new vocab review mode"**
→ Branch 1. `state:draft`. The `[WIP]` tag is the signal.

## Output discipline

Every dispatch ends with **one** of:

- A label edit + a short comment (Branches 1, 2, 3).
- A label edit only (Branch 4).

That's it. Do not post your reasoning, do not narrate the decision tree you walked, do not summarize the issue back at the user. The whole skill exists to keep new issues moving without making the human read your scratch work.

## Failure handling

- If the issue was closed between dispatch and your read: silently exit. The fabric's closure-detection path will handle the bookkeeping on the next tick.
- If `gh issue edit` fails for a label that doesn't exist on the project: the project hasn't yet run `fabric setup-labels`. Comment the label name + this fact, and exit. The fabric will retry on the next tick.
- If you genuinely cannot decide between two branches after re-reading: prefer Branch 2 (ask one question) over guessing. The cost of one Q&A round is one tick.
