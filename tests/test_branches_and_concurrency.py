from __future__ import annotations

import asyncio
import concurrent.futures
import time
from dataclasses import dataclass

from papertrail import Trail


@dataclass
class ParallelTurnState:
    text: str
    answer: str = ""
    teach: str = ""


def test_thread_pool_branches_merge_cleanly() -> None:
    state = ParallelTurnState(text="What does decir mean?")
    trail = Trail(state, name="parallel-threads")

    def run_branch(branch_name: str, field_name: str, delay_s: float) -> Trail[dict[str, str]]:
        child_state = {field_name: ""}
        child = trail.branch(branch_name, state=child_state)
        with child.stage(branch_name):
            time.sleep(delay_s)
            child.decide(f"{branch_name}_gate", outcome="run", reason="eligible clause present")
            child.set(field_name, f"output-{branch_name}", reason="synthesized response")
        return child

    with trail.stage("parallel_micro_calls"):
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            f_ans = pool.submit(run_branch, "answer_branch", "answer", 0.01)
            f_teach = pool.submit(run_branch, "teach_branch", "teach", 0.02)
            c_ans = f_ans.result()
            c_teach = f_teach.result()
        trail.merge(c_ans)
        trail.merge(c_teach)

    entries = trail.entries
    assert [e.seq for e in entries] == [1, 2, 3, 4]
    assert [e.branch for e in entries] == [
        "answer_branch",
        "answer_branch",
        "teach_branch",
        "teach_branch",
    ]
    assert len(trail.stages) == 3
    assert {s.stage for s in trail.stages} == {"answer_branch", "teach_branch", "parallel_micro_calls"}


def test_asyncio_tasks_isolate_contextvar_stages() -> None:
    trail = Trail({"a": 0, "b": 0}, name="async-turn")

    async def worker(name: str, key: str, value: int, delay: float) -> None:
        with trail.stage(f"stage_{name}"):
            trail.decide(f"gate_{name}", outcome=True, reason="async check")
            await asyncio.sleep(delay)
            trail.set(key, value, reason=f"updated in {name}")

    async def runner() -> None:
        await asyncio.gather(
            worker("alpha", "a", 10, 0.02),
            worker("beta", "b", 20, 0.01),
        )

    asyncio.run(runner())

    by_field = {e.field: e for e in trail.entries}
    assert by_field["gate_alpha"].stage == "stage_alpha"
    assert by_field["a"].stage == "stage_alpha"
    assert by_field["gate_beta"].stage == "stage_beta"
    assert by_field["b"].stage == "stage_beta"
    assert len(trail.stages) == 2
