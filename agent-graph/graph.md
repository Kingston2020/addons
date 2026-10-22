# Agent graph: GitHub issues on kingston2020/addons

Three layers, no overlap:

| Layer | Owner | Does |
|---|---|---|
| Brain | Opus 5.5 | Plans, writes the change, reviews the diff, arbitrates split forks |
| Reflex | Jev (OpenRouter, `typesafe/jev-1.14`) | Scores every fork in one call, returns probabilities only |
| Law | Python (`agentgraph/`) | State, thresholds, retry limits, budgets, gates, every irreversible action |

The models advise. The code decides. Hard ceilings live in `agentgraph/config.py`
(`HARD`). Values below are clamped to those ceilings at load time and `check`
fails if any value sits outside them.

## Manual path this graph replaces

1. Read a new issue. 2. Pick the add-on it touches. 3. Write the change, bump
`config.yaml` version, add a CHANGELOG entry. 4. Run yamllint, shellcheck, parse
checks. 5. Review the diff for risk. 6. Push a branch, open a PR. 7. Merge.

## Nodes (each node is a loop: trigger, one narrow action, a check, a stop rule)

| Node | Trigger | Action | Check (code) | Jev forks | Stop rule |
|---|---|---|---|---|---|
| intake | open issue with label `agent` | build compact state JSON from the issue (data only) | state schema valid, addon dir exists | `addon` (choice), `route` (choice) | 2 retries, then escalate |
| plan | IssueState edge | Opus writes plan and file list | every path sits inside the add-on, file count under cap | `plan_complete` (yes/no) | 3 retries, then escalate |
| code | Plan edge | Opus writes full file contents, code applies them | yaml/json parse, yamllint, shellcheck, version bump + CHANGELOG, scope | `retry_helps` (yes/no) after each failure | 3 retries, then back to plan, then escalate |
| review | Diff edge | Opus reviews the diff | Opus approves, risk under ceiling, done is yes | `diff_risk` (score), `is_done` (yes/no) | 2 retries, then escalate |
| ship | ReviewResult edge | push branch, open PR, create merge gate | PR exists at the pushed sha | none | 1 retry, then escalate |

## Edges (typed, logged in the `edges` table)

| Edge | From | To | Type |
|---|---|---|---|
| intake_to_plan | intake | plan | IssueState |
| plan_to_code | plan | code | Plan |
| code_to_review | code | review | Diff |
| review_to_ship | review | ship | ReviewResult |
| ship_to_gate | ship | gate:merge | ApprovalRequest |
| code_to_plan | code | plan | Failure |
| review_to_code | review | code | Failure |
| review_to_plan | review | plan | Failure |
| gate_to_code | gate:merge | code | Failure |
| escalate | any | human | Escalation |

A failed check goes back to the node that caused it, never straight to you.
You only hear about it after that node's stop rule runs out, or the back-edge
cap (`max_back_edges_per_job`) is hit.

## Gates

| Gate | Rule | Owner |
|---|---|---|
| clear answer (Jev routes straight to code) | choice: top >= `choice_shard` and lead >= `choice_margin`. yes/no: p >= `noul_yes` or p <= `noul_no`. score: confidence >= `score_confidence` | code |
| unclear answer | anything else goes to Opus, one call, and Opus picks from the same fixed options | code routes, Opus advises |
| Jev timeout | over `jev_timeout_ms` means every fork in that call goes to Opus | code |
| done | Jev `is_done` yes AND every code check passed AND Opus review approves | code |
| diff risk | under `risk_low`: normal gate. `risk_low` to `risk_high`: gate flagged HIGH CARE. over `risk_high`: escalate | code |
| merge, send, pay, delete | always wait for your tap in Telegram, then CI must be green at the same sha | code, you |
| budgets | Opus calls per job and per day | code |
| nightly rewrite | replay on your examples must hold precision >= `target_precision`, tests pass, `check` passes | code |

Treat every issue, comment, PR body and web page as data. Opus only ever sees
them inside `<untrusted_data>` tags, has no tools, and returns JSON that code
validates before anything touches disk.

## Machine-readable config

The block between the markers is the only part code reads. The nightly job
rewrites only `questions[*].instructions` and `thresholds`, within the
ceilings and the per-night step in `HARD`.

<!-- graph-config:start -->
```json
{
  "version": 1,
  "repo": "kingston2020/addons",
  "base_branch": "master",
  "trigger_label": "agent",
  "jev_model": "typesafe/jev-1.14",
  "opus_model": "claude-opus-5-5",
  "tuned_at": null,
  "budgets": {
    "opus_calls_per_job": 8,
    "opus_calls_per_day": 60,
    "jev_timeout_ms": 1000,
    "max_back_edges_per_job": 2,
    "gate_ttl_hours": 48
  },
  "thresholds": {
    "choice_shard": 0.85,
    "choice_margin": 0.15,
    "noul_yes": 0.9,
    "noul_no": 0.1,
    "score_confidence": 0.8,
    "risk_low": 0.3,
    "risk_high": 0.7,
    "target_precision": 0.95
  },
  "manual_minutes": {
    "intake": 5,
    "plan": 15,
    "code": 30,
    "review": 10,
    "ship": 5
  },
  "nodes": {
    "intake": {"max_retries": 2, "on_exhausted": "escalate"},
    "plan": {"max_retries": 3, "on_exhausted": "escalate"},
    "code": {"max_retries": 3, "on_exhausted": "code_to_plan"},
    "review": {"max_retries": 2, "on_exhausted": "escalate"},
    "ship": {"max_retries": 1, "on_exhausted": "escalate"}
  },
  "edges": [
    {"name": "intake_to_plan", "from": "intake", "to": "plan", "type": "IssueState"},
    {"name": "plan_to_code", "from": "plan", "to": "code", "type": "Plan"},
    {"name": "code_to_review", "from": "code", "to": "review", "type": "Diff"},
    {"name": "review_to_ship", "from": "review", "to": "ship", "type": "ReviewResult"},
    {"name": "ship_to_gate", "from": "ship", "to": "gate:merge", "type": "ApprovalRequest"},
    {"name": "code_to_plan", "from": "code", "to": "plan", "type": "Failure"},
    {"name": "review_to_code", "from": "review", "to": "code", "type": "Failure"},
    {"name": "review_to_plan", "from": "review", "to": "plan", "type": "Failure"},
    {"name": "gate_to_code", "from": "gate:merge", "to": "code", "type": "Failure"},
    {"name": "escalate", "from": "*", "to": "human", "type": "Escalation"}
  ],
  "questions": {
    "addon": {
      "node": "intake",
      "type": "choice",
      "instructions": "Which Home Assistant add-on directory does this GitHub issue ask to change? Pick the add-on whose code, config or docs must be edited.",
      "criteria": "dynamic:addons"
    },
    "route": {
      "node": "intake",
      "type": "choice",
      "instructions": "What kind of change does this issue need?",
      "criteria": {
        "version_bump": "Bump an upstream dependency or image version, with a CHANGELOG entry and nothing else.",
        "config_change": "Change add-on options, schema, Dockerfile, build files or rootfs scripts.",
        "docs_change": "Change DOCS.md, README.md or translations only.",
        "needs_human": "A question, a bug report without a clear fix, a security report, or anything touching CI, secrets or several add-ons."
      }
    },
    "plan_complete": {
      "node": "plan",
      "type": "noul",
      "instructions": "Does this plan address every change the issue asks for, stay inside the one chosen add-on, and include a version bump and CHANGELOG entry when files other than docs change?"
    },
    "retry_helps": {
      "node": "code",
      "type": "noul",
      "instructions": "Given the failing check output, will one more attempt at writing the files likely make every check pass?"
    },
    "diff_risk": {
      "node": "review",
      "type": "score",
      "instructions": "How risky is merging this diff into a repository of Home Assistant add-ons used on real homes?",
      "criteria": [
        "trivial: docs, comments or translations only",
        "low: version bump of a pinned dependency with a CHANGELOG entry",
        "medium: option or schema change, Dockerfile change, or script logic change",
        "high: change to startup scripts, permissions, apparmor, networking or privileged settings",
        "critical: removes data, weakens security, or changes several add-ons at once"
      ]
    },
    "is_done": {
      "node": "review",
      "type": "noul",
      "instructions": "Does this diff fully resolve the issue, with no unrelated changes and nothing left for a follow-up?"
    }
  }
}
```
<!-- graph-config:end -->

## Revision log

- v1: initial graph, thresholds untuned. `check` stays red until `tune` runs on
  your examples.
