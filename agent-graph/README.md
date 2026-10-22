# agent-graph

A graph that turns GitHub issues labelled `agent` on `kingston2020/addons` into
pull requests, and merges them only after you tap Approve in Telegram.

- Opus 5.5 plans, writes and reviews.
- Jev (via OpenRouter) scores every fork in one call, returning probabilities only.
- Python owns state, thresholds, retries, budgets and every irreversible action.

Read `graph.md` first. It holds the nodes, edges, gates and the config block the code runs on.

## Layout

```
graph.md                 nodes, edges, gates, live config (nightly rewrites part of it)
agentgraph/config.py     HARD limits, IRREVERSIBLE actions, protected paths, graph validation
agentgraph/engine.py     the five nodes and the edge, retry and back-edge rules
agentgraph/router.py     clear answer -> code, unclear answer -> Opus
agentgraph/jev.py        Jev client (typesafe-sdk, OpenRouter base URL), zero SDK retries
agentgraph/opus.py       Opus client, no tools, JSON output, untrusted data fenced
agentgraph/gates.py      merge gate: your approval, unchanged PR head, green CI, then merge
agentgraph/workspace.py  git worktree per issue, path guards, yamllint/shellcheck/version checks
agentgraph/dashboard.py  live dashboard (server-sent events), token required
agentgraph/nightly.py    nightly review: Opus proposes, code clamps, replay decides
agentgraph/tune.py       threshold tuning on your labelled examples
agentgraph/check.py      final check, WHAT COULD BREAK THIS WEEK?, unattended refusal
deploy/                  systemd units, timers, install.sh for the VPS
tests/                   27 tests, all offline
```

## Try it with no keys

```
pip install -r requirements.txt
python -m agentgraph demo            # one issue end to end, gate, merge, report
python -m agentgraph demo --serve    # same, then open http://127.0.0.1:8766/?token=demo
python -m pytest -q tests
```

## Keys (.env only)

`cp .env.example .env && chmod 600 .env`, then fill in:

| Key | Where |
|---|---|
| `OPENROUTER_API_KEY` | openrouter.ai, Keys. This is your Jev key. |
| `ANTHROPIC_API_KEY` | console.anthropic.com |
| `GITHUB_TOKEN` | Fine-grained PAT, repo `kingston2020/addons` only: Contents RW, Pull requests RW, Issues R, Checks R |
| `TELEGRAM_BOT_TOKEN` | @BotFather, `/newbot` |
| `TELEGRAM_CHAT_ID` | Send `/start` to your bot, then open `https://api.telegram.org/bot<TOKEN>/getUpdates` and copy `message.chat.id` |
| `DASHBOARD_TOKEN` | `openssl rand -hex 24` |

Keys never go in code, graph.md, logs, issues or chat. The logger strips any secret
value it sees, git auth goes through env vars and never through the remote URL, and
`check` fails if `.env` is readable by anyone but its owner.

## Tune before you trust it

1. Copy `examples/labelled.example.jsonl` to `examples/labelled.jsonl`.
2. Add at least 20 of your own real issues with the answer you would give
   (`route`, `addon`, `plan_complete`, `is_done`, `diff_risk`). Mix easy and hard ones.
3. `python -m agentgraph tune --write` picks the loosest threshold that still keeps
   clear answers at `target_precision` (0.95), never below the floors in `HARD`.

## Deploy on the VPS (Ubuntu 22.04 or 24.04)

```
ssh root@<vps>
git clone --branch claude/agent-workflow-graph-5g1gl1 https://github.com/kingston2020/addons.git /tmp/addons
BRANCH=claude/agent-workflow-graph-5g1gl1 bash /tmp/addons/agent-graph/deploy/install.sh
nano /opt/agent-graph/app/agent-graph/.env       # paste keys here, on the VPS
BRANCH=claude/agent-workflow-graph-5g1gl1 bash /tmp/addons/agent-graph/deploy/install.sh
```

After the PR merges, drop `BRANCH=...` and the `--branch` flag.

What install.sh sets up:

- Timezone `America/Jamaica`, a `agentgraph` system user, a venv in `/opt/agent-graph/venv`.
- `agent-graph.service`: the daemon, `Restart=always`, `RestartSec=10`, no start limit, so it comes back after crashes and reboots.
- `agent-graph-nightly.timer`: 03:17 nightly review.
- `agent-graph-report.timer`: 07:43 daily report to Telegram.

Operate it:

```
systemctl status agent-graph
journalctl -u agent-graph -f
sudo -u agentgraph /opt/agent-graph/venv/bin/python -m agentgraph check   # run from /opt/agent-graph/app/agent-graph
```

Dashboard: it binds to 127.0.0.1 only. From your laptop or phone (Termius, Blink):

```
ssh -L 8765:127.0.0.1:8765 root@<vps>
open http://127.0.0.1:8765/?token=<DASHBOARD_TOKEN>
```

Tailscale works too: install it on the VPS and keep the bind on 127.0.0.1 behind `tailscale serve`.

## Telegram alerts

You get a message for every gate (with Approve/Reject buttons), every escalation,
every error, the nightly result and the daily report. Buttons only work from your
`TELEGRAM_CHAT_ID`. An approval merges only if the PR head still matches the sha
you approved and every check run is green.

## Going unattended

`UNATTENDED=1` in `.env` makes the daemon run `check` at start and refuse to boot
until every blocker clears: thresholds tuned on 20+ of your examples, fork history
from a supervised day, all keys present, `.env` locked down, hard limits inside `HARD`.
Gates still wait for you either way.

## Commands

```
python -m agentgraph run [--pick-up]   daemon: poll issues, run jobs, run approved gates, dashboard
python -m agentgraph dashboard         dashboard only
python -m agentgraph check             final check, exits 2 when unattended is refused
python -m agentgraph tune [--write]    tune thresholds on examples/labelled.jsonl
python -m agentgraph nightly [--dry-run]
python -m agentgraph report [--send]
python -m agentgraph demo [--serve]
```
