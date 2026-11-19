# papertrail

Zero-dependency per-turn state and decision provenance ledger for Python pipelines.

## Why it exists

Once a pipeline combines heuristics, classifiers, and LLM calls, plain logs make it hard to answer two questions after a bad output:

1. **What changed on the turn state, and from what line of code?**
2. **Which gates, thresholds, or skipped branches ran — and with what scores/reasons?**

`papertrail` wraps a turn's state (`dataclass`, `dict`, or plain object) with an append-only `Trail` ledger that captures state mutations, skipped branches, stage timings, caller `file:line`, and parallel worker branches, then exports JSONL logs and Markdown timelines.

## Usage

### Explicit changes, ambient recording, and stage allowlists

```python
from dataclasses import dataclass, field
import papertrail
from papertrail import Trail

@dataclass
class TurnState:
    user_text: str
    signals: dict = field(default_factory=dict)
    route: str = "default"
    reply: str = ""

def deep_classifier(score: float) -> None:
    # Works anywhere inside `with trail.activate():` without passing trail through every call.
    if score >= 0.70:
        papertrail.decide("route_question", outcome="answer", reason="score >= threshold", evidence={"score": score})
        papertrail.set("route", "answer_question", reason="classifier routed to QA branch")
    else:
        papertrail.decide("route_question", outcome="skip", reason="score below threshold", evidence={"score": score})

state = TurnState(user_text="How do I conjugate hablar?")
external_plan = {"step": "show", "target": "greet"}

trail = Trail(
    state,
    name="turn-14",
    input_summary={"user_text": state.user_text},
    watch_default=("route", "reply"),
    watchers={"plan": lambda: external_plan},
)

with trail.activate():
    with trail.stage("classifier"):
        deep_classifier(0.88)
        external_plan["step"] = "use"  # Captured as a compact key-level diff on stage exit.

print(trail.stage_timings())
print(trail.to_markdown())
trail.write_jsonl("turns.jsonl")
```

### Zero-cost disabled mode (`NullTrail` / `enabled=False`)

```python
from papertrail import NullTrail, Trail

trail = Trail(state, enabled=True) if debug_enabled else NullTrail(state)
```

When `enabled=False`, `decide()`, `note()`, `record_change()`, `activate()`, and `stage()` become no-ops with no snapshotting or locking while `trail.set(...)` still updates `state`.

### Parallel branches (`ThreadPoolExecutor` / `asyncio`)

```python
def run_worker(branch_name: str):
    child = trail.branch(branch_name)
    with child.activate():
        with child.stage(branch_name):
            papertrail.decide("worker_ok", outcome=True, reason="completed")
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
