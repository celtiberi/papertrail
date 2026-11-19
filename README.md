# papertrail

Zero-dependency per-turn state and decision provenance ledger for Python pipelines.

## Why it exists

Once a pipeline combines heuristics, classifiers, and LLM calls, plain logs make it hard to answer two questions after a bad output:

1. **What changed on the turn state, and from what line of code?**
2. **Which gates, thresholds, or skipped branches ran — and with what scores/reasons?**

`papertrail` wraps a turn's state (`dataclass`, `dict`, or plain object) with an append-only `Trail` ledger that captures state mutations, skipped branches, stage timings, caller `file:line`, and parallel worker branches, then exports JSONL logs and Markdown timelines.

## Usage

### Explicit changes and decisions

```python
from dataclasses import dataclass, field
from papertrail import Trail

@dataclass
class TurnState:
    user_text: str
    signals: dict = field(default_factory=dict)
    route: str = "default"
    reply: str = ""

state = TurnState(user_text="How do I conjugate hablar?")
trail = Trail(state, name="turn-14", input_summary={"user_text": state.user_text})

with trail.stage("classifier"):
    score = 0.88
    if score >= 0.70:
        trail.decide("route_question", outcome="answer", reason="score >= threshold", evidence={"score": score})
        trail.set("route", "answer_question", reason="classifier routed to QA branch")
    else:
        trail.decide("route_question", outcome="skip", reason="score below threshold", evidence={"score": score})

print(trail.to_markdown())
trail.write_jsonl("turns.jsonl")
```

### Incremental adoption with stage watch diffing

Wrap existing code without rewriting every assignment:

```python
with trail.stage("legacy_intake", watch="*"):
    legacy_intake(state)  # Direct attribute/dict mutations are captured as `diff` entries on exit.
```

Fields already updated via `trail.set(...)` inside the stage are not double-logged by `watch`.

### Parallel branches (`ThreadPoolExecutor` / `asyncio`)

```python
def run_worker(branch_name: str):
    child = trail.branch(branch_name)
    with child.stage(branch_name):
        child.decide("worker_ok", outcome=True, reason="completed")
    return child

for child in completed_children:
    trail.merge(child)
```

### CLI

```sh
papertrail show turns.jsonl --index -1
papertrail show turns.jsonl --decisions
papertrail show turns.jsonl --field route --json
```
