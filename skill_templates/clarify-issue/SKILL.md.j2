---
name: clarify-issue
description: Use this skill when, partway through working on a GitHub issue, you hit genuine ambiguity that you cannot resolve from the body, the codebase, or `dev-flow` rules. Posts a focused question as an issue comment, flips the state label to `state:clarification-needed`, and stops work on the issue. Do not invoke for self-resolvable uncertainty.
version: 1.0.0
---

# clarify-issue

A worker agent's escape hatch when an issue's body is missing a load-bearing decision. Posts the specific question, parks the issue, and stops.

## When to invoke

You are mid-implementation on issue #N and one of these is true:
- The issue body is silent on a behaviour choice that materially changes the diff (e.g. "should the new field be nullable?", "default tone for new chats — funny or mixed?").
- Two reasonable interpretations of the body lead to different module changes.
- A constraint in the codebase (e.g. existing schema, an FSRS invariant) collides with what the body asks for.

## When **not** to invoke

- You can resolve the question by reading more code, the plan doc, or `CLAUDE.md`. Read first; clarify only if those are silent too.
- You are uncertain about a small naming choice. Pick one and proceed; the user can rename in review if they care.
- You haven't started yet. If the body is unclear before you begin, ask the user in chat — don't post an issue comment for the planning round.

The bar is: *would another reasonable agent make a different decision here, and would that decision survive into a different PR shape?*

## The flow

### 1. Frame the question precisely

Bad: "I'm not sure about the design."
Good: "Should `/resetvocab` also clear `push_log` for that chat, or only `words`?"

One question is best. Two if they're tightly coupled. Never a list of vague items.

### 2. Post it as an issue comment

```bash
gh issue comment <N> --body "$(cat <<'EOF'
**Clarification needed before proceeding**

<the precise question>

Context: <one sentence — what you've already decided, what's left>.
Proceeding once labelled back to `state:in-progress`.
EOF
)"
```

### 3. Flip the state label

```bash
gh issue edit <N> \
  --remove-label "state:in-progress" \
  --add-label "state:clarification-needed"
```

### 4. Stop

Do **not** keep coding speculatively while waiting. Do **not** open the PR. Do **not** push the branch yet — your in-progress work stays local. Tell the user, in one sentence, what you posted and that you're paused.

## After the user answers

When the user answers (in chat or by editing the issue) and tells you to resume:
1. Flip the label back: `--remove-label "state:clarification-needed" --add-label "state:in-progress"`.
2. Resume from where you stopped. Do not re-run earlier steps that already succeeded.

## Hard rules

- **Don't** close the issue.
- **Don't** convert the issue to a draft / change its title.
- **Don't** open multiple clarification comments on the same issue without the user's input — if you have a follow-up, reply to your own comment as a thread.
- **Don't** apply `state:clarification-needed` if the issue isn't already `state:in-progress` (you shouldn't be working on it in that case).
