---
name: epic-decompose
description: Decomposition stage for `type:epic` issues at `state:needs-decompose`. Multi-round Socratic Q&A with the user (one round per dispatch), then proposes a child-issue list, awaits explicit approval (`/decompose-ok`), files children with `Refs #<parent>`, and parks the parent at `state:tracking`. Children are filed B3-style — child #1 at `state:needs-planning`, the rest at `state:draft` — so the fabric's coordinator can release them one at a time as each predecessor closes. Considers documentation impact during decomposition so the epic doesn't close with stale docs. Does NOT cut branches, write code, or open PRs.
version: 2.1.0
---

# epic-decompose

Decomposition stage for `type:epic` parents. Splits the epic into smaller child issues that the existing pipeline ships one at a time, in the order you propose. One agent invocation = one round; a full decomposition typically spans several Q&A rounds before a proposal is approved.

## Hard boundaries

You MAY:
- `gh issue view`, `gh issue list`, `gh issue create` (file children).
- `gh issue comment`, `gh issue edit` (labels) on the parent.
- Read code to ground your understanding of where the change would land.

You MUST NOT:
- Edit code, create branches, commit, push, or open PRs. Decomposition is research and planning only — implementation belongs to plan-exec on each *child* issue.
- Decompose anything that is **not** labelled `type:epic`. Bounce back to plan-exec if you were dispatched on the wrong issue.
- File child issues until the user has explicitly approved your latest proposal with the `/decompose-ok` magic phrase.

## Inputs and exit conditions

Dispatched on a `type:epic` parent at `state:needs-decompose`. That's the entry state on the very first dispatch (set by `qualify-issue`) and also the resume state after the user answers a clarification or asks for proposal revisions.

Exits the run by flipping the state label to one of:
- `state:clarification-needed` — you posted a question, awaiting answer.
- `state:awaiting-decompose-approval` — you posted a proposed child list, awaiting `/decompose-ok`.
- `state:tracking` — children filed, parent now waits for them to merge. The fabric's coordinator advances child #2 to planning when child #1 closes, and so on; when the last child closes the parent auto-closes too.
- `state:blocked` — round cap exhausted or unresolvable error (rare).

## The decision tree (do this every dispatch)

Read the issue body and **all** comments. Then walk this tree top-down — first match wins:

1. **Approval detected.** A user comment that contains `/decompose-ok` exists and is dated *after* your most recent `<!-- agent-decompose v1 -->` proposal comment.
   → File the children (see §4), flip to `state:tracking`, exit.

2. **Round cap hit.** You've already posted ≥ 8 `<!-- agent-decompose-q v1 -->` question comments.
   → Force a proposal anyway, but tag it `**Confidence: low — round cap reached.**` (see §3 with the tag added), flip to `state:awaiting-decompose-approval`, exit.

3. **Ready to propose.** You have enough information to draft a credible child-issue list — every must-answer question from §5 has either been answered or is genuinely deferrable.
   → Post the proposal (see §3), flip to `state:awaiting-decompose-approval`, exit.

4. **Need more information.** Default branch.
   → Post one focused question (see §2), flip to `state:clarification-needed`, exit.

**Bias toward more questions, not fewer.** The cost of a wrong cut-line is multiple ill-shaped child issues. The cost of one extra Q&A round is one tick. Err on the side of asking.

## 1. First step on every dispatch — flip to in-progress

```bash
gh issue edit <N> \
  --remove-label "state:needs-decompose,state:awaiting-decompose-approval" \
  --add-label "state:in-progress"
```

`gh` ignores `--remove-label` for labels that aren't present, so the same command works from either entry state.

## 2. Question round

One question per round. The single most load-bearing unknown — what would change the *shape* of the decomposition if answered the other way.

Bad: "What are your thoughts on the architecture?"
Good: "Should the part-of-speech classifier run at `/add` time (one-shot, stored on the row) or lazily on first push (cached)? The two paths split the work very differently."

Marker `<!-- agent-decompose-q v1 -->` is mandatory — the round counter and future dispatches read it.

```bash
gh issue comment <N> --body "$(cat <<'EOF'
<!-- agent-decompose-q v1 -->
**Decomposition question (round <K> of up to 8)**

<the precise question>

Context: <one sentence — what you've already concluded, what depends on this>.

When you answer, flip the label back to `state:needs-decompose` to re-enter the loop.
EOF
)"
```

Then flip the state and exit:

```bash
gh issue edit <N> \
  --remove-label "state:in-progress" \
  --add-label "state:clarification-needed"
```

Print `paused at round <K>: clarification posted` and stop. Do not propose in the same round you ask.

## 3. Propose round

A proposal is a complete, dependency-ordered child list. Every child should be small enough that plan-exec can ship it as one focused PR (~300 LOC, one area), with crisp acceptance criteria.

Marker `<!-- agent-decompose v1 -->` is mandatory.

```bash
gh issue comment <N> --body "$(cat <<'EOF'
<!-- agent-decompose v1 -->
## Proposed decomposition for #<N>

**Confidence:** high | medium | low — round cap reached (use this tag if you hit §H2).

**One-line shape:** <how the children fit together>.

### Child 1 — <imperative title>
- **Labels:** `type:feat`, `priority:high`, `area:vocab`
- **Goal:** <one sentence>
- **Acceptance criteria:**
  - AC1: <given X, when Y, then Z>
  - AC2: <invariant>
- **Depends on:** none (or "child #2 must merge first")
- **Out of scope:** <what is explicitly NOT in this child>

### Child 2 — ...

---

**Reply `/decompose-ok` to approve.** Then flip `state:awaiting-decompose-approval` → `state:needs-decompose` and the next dispatch will file these as child issues. To request changes, comment your feedback and flip back to `state:needs-decompose` — I'll revise.
EOF
)"
```

Then:

```bash
gh issue edit <N> \
  --remove-label "state:in-progress" \
  --add-label "state:awaiting-decompose-approval"
```

Print `proposal posted: <N> children, awaiting approval` and exit.

**Sizing rule of thumb.** Aim for 3–8 children. Fewer than 3 → not really an epic; suggest just unlabelling `type:epic` and letting plan-exec handle it. More than 8 → either the epic is too big to plan in one pass (suggest splitting the epic itself), or you're slicing too thin (consolidate).

**Docs hygiene (when applicable).** If the epic adds, removes, or renames user-facing surface — slash commands, `/help` text, README sections, in-app banners, `/start` onboarding flow, configuration env vars, public CLI flags — the documentation needs to land somewhere. Two acceptable shapes:
- **Trailing `type:docs` child** — a small, focused child at the end of the sequence that updates README + `/help` + any onboarding copy in one PR. Use this when the docs touch multiple files or when several feature children touch the same surface.
- **Doc touch-ups inside feature children** — fold the docs work into the relevant feature child's acceptance criteria (e.g. AC: `/help` lists the new command). Use this when the docs change is small and tightly coupled to one feature.

Either is fine. The sin is letting the epic auto-close with stale docs because no child owned them. If the epic is purely internal (refactor, infra, test coverage) — no user-facing surface change — say so explicitly in the proposal's "One-line shape" and skip the docs child.

## 4. File-children round (after `/decompose-ok`)

Triggered when §A1 of the decision tree matches. For each child in the most recent proposal, **in the order you listed them**:

- The **first** child gets `state:needs-planning` — that's the one the pipeline will pick up immediately.
- Every **subsequent** child gets `state:draft` — held until its predecessor closes. The fabric's coordinator (in `scheduler.py`) detects each child's closure, parses `Refs #<parent>` from its body, and flips the next `state:draft` sibling to `state:needs-planning` automatically. When the last child closes, the parent auto-closes too.

The `Refs #<N>` line in the body is **load-bearing** — the coordinator parses it. Don't drop it, don't reword it.

```bash
# Child #1 — the kick-off:
gh issue create \
  --title "<imperative title>" \
  --label "type:feat,priority:medium,area:vocab,state:needs-planning" \
  --body "$(cat <<EOF
<one-paragraph problem statement, lifted from the proposal>

## Acceptance criteria
- AC1: ...
- AC2: ...

## Out of scope
- ...

Part of epic #<N>.
Refs #<N>
EOF
)"

# Children #2..K — held until their predecessor closes:
gh issue create \
  --title "<imperative title>" \
  --label "type:feat,priority:medium,area:vocab,state:draft" \
  --body "$(cat <<EOF
<one-paragraph problem statement>

## Acceptance criteria
- AC1: ...

## Out of scope
- ...

Part of epic #<N>.
Refs #<N>
EOF
)"
```

Capture each new issue number from the URL `gh issue create` prints. Then post one closing comment on the parent that lists the child links and flip to `state:tracking`:

```bash
gh issue comment <N> --body "$(cat <<EOF
<!-- agent-decompose-filed v1 -->
**Decomposition filed.** Children (will be released one at a time as each predecessor closes):

- #<C1> — <title> · `state:needs-planning` (active)
- #<C2> — <title> · `state:draft` (held)
- #<C3> — <title> · `state:draft` (held)
- ...

The fabric will advance the next held child to `state:needs-planning` automatically when each closes. Parent auto-closes once the last child is done.
EOF
)"

gh issue edit <N> \
  --remove-label "state:in-progress" \
  --add-label "state:tracking"
```

Print `filed <K> children for epic #<N>: <numbers>` and exit.

## 5. What "must-answer" questions look like for an epic

A non-exhaustive checklist of the kinds of unknowns to clear before proposing. Walk through these mentally on every Q-round and pick the most load-bearing unanswered one:

- **Scope edges.** What is explicitly *not* in scope for this epic? (Without this you'll over-decompose.)
- **Storage shape.** New tables / columns? Migration concerns?
- **User-facing surface.** New slash commands? Inline UI? `/start` flow changes?
- **Dependency order.** What must merge before what? Are any children blockers vs leaves?
- **Integration points.** Does this touch the LLM call site, the scheduler, the translator, the FSRS state machine?
- **Test seams.** Anything that needs a refactor *first* to be testable, before the feature children land?
- **External services / config.** New env vars? New API keys? Quotas?
- **Naming.** Names of new commands, modules, labels — pick *or* ask.
- **User-facing docs.** Which docs (README, `/help` command output, in-app banners, `/start` onboarding) describe the surface this epic touches? Are they out-of-date or generic enough to survive without changes? If non-trivial, the docs deserve their own child (or an explicit AC on the relevant feature child) — see "Docs hygiene" in §3.

## After-the-loop notes

- **You don't run plan-exec on the parent.** Once children are filed, the parent's job is over. plan-exec will be dispatched separately on each child by the normal pipeline.
- **Don't overlap with `clarify-issue`.** That skill is for *implementation* clarifications during plan-exec; epic-decompose has its own Q&A flow with its own marker so the two histories don't tangle.
- **Don't cycle past 8 questions** without forcing a proposal. The round cap exists so an over-cautious agent can't deadlock the epic.
- **One commit-style discipline.** Every child issue's acceptance criteria should map cleanly to tests in test-writer's spec format (numbered, behavioural, not implementation). Test-writer reads only the AC block — sloppy ACs here become sloppy tests downstream.
