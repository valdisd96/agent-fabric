# agent-fabric

A Telegram-controlled service that orchestrates autonomous coding-agent pipelines across multiple GitHub repos.

Each registered project runs its own pipeline (e.g. `plan-exec` → `test-writer` → `review-pr`) defined by per-repo agent scripts and Claude skills. The fabric polls GitHub issue labels, dispatches the right script for each issue's state, manages retries and cycle limits, and surfaces approvals/clarifications to a single Telegram bot for one-tap action from your phone.

The first project it manages is [`teach-me-eng-bot`](https://github.com/valdisd96/teach-me-eng-bot), whose three-stage pipeline (`workflow.md` in that repo) is the worked example the fabric is shaped around.

See [`DESIGN.md`](./DESIGN.md) for the v1 architecture, locked decisions, and ticket scope.

## Status

v1 in progress. Tickets are filed as GitHub issues.

## Quickstart (after v1 lands)

```
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env                                          # fill TG token, gh PAT, allowed user ids
cp projects.example.yaml projects.yaml                        # add your repos
uvicorn agent_fabric.main:app --host 0.0.0.0 --port 8090
```

## License

MIT (placeholder — confirm before publishing).
