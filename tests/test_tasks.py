import pytest

from codeagent.llm import call
from codeagent.planning import checklist

from helpers import FIXED, PLAN, Crash, answer, calls, tool_messages


def execution_script():
    return [
        calls(call("read_file", "p1", path="order.py")),
        calls(call("submit_plan", "p2", **PLAN)),
        calls(call("read_file", "t1", path="order.py")),
        calls(call("complete_task", "t2", task_id="T1", summary="read order.py")),
        calls(call("complete_task", "t3", task_id="T2", summary="claims it is fixed")),
        calls(call("write_file", "t4", path="order.py", content=FIXED)),
        calls(call("complete_task", "t5", task_id="T2", summary="fixed")),
        calls(call("Bash", "t6", command="python3 -m unittest -v", cwd=".")),
        calls(call("complete_task", "t7", task_id="T3", summary="tests pass")),
        answer("Done"),
    ]


def start_and_approve(project, script, **options):
    runtime = project.runtime(script, **options)
    run = runtime.advance(runtime.start("fix", mode="auto", plan=True))
    runtime.approve(run)
    return runtime, run


def test_tasks_run_in_dependency_order_with_verified_results(project):
    runtime, run = start_and_approve(project, execution_script())
    runtime.advance(run)

    assert run["status"] == "finished"
    assert [t["status"] for t in run["tasks"].values()] == ["done", "done", "done"]
    assert run["tasks"]["T1"]["evidence"]["call_id"] == "t1"
    assert run["tasks"]["T2"]["evidence"]["call_id"] == "t4"
    assert run["tasks"]["T3"]["evidence"]["call_id"] == "t6"
    assert "is not complete" in tool_messages(run)["t3"]


def test_every_execution_call_is_bound_to_a_task(project):
    runtime, run = start_and_approve(project, execution_script())
    runtime.advance(run)
    task_ids = {call_id: c["task_id"] for call_id, c in run["calls"].items()}
    assert task_ids == {
        "p1": None, "p2": None,
        "t1": "T1", "t2": "T1",
        "t3": "T2", "t4": "T2", "t5": "T2",
        "t6": "T3", "t7": "T3",
    }


def test_dependent_task_is_not_started_early(project):
    runtime, run = start_and_approve(project, [
        calls(call("read_file", "p1", path="order.py")),
        calls(call("submit_plan", "p2", **PLAN)),
        calls(call("complete_task", "x1", task_id="T2", summary="skip ahead")),
    ])
    with pytest.raises(RuntimeError):
        runtime.advance(run)
    assert "T2 is not the current task (T1)" in tool_messages(run)["x1"]
    assert run["tasks"]["T2"]["status"] == "pending"


def test_command_check_needs_a_run_after_the_last_change(project):
    plan = {**PLAN, "steps": [
        {"id": "T1", "description": "Run tests", "tools": ["Bash"], "depends_on": [],
         "check": {"kind": "command_ok", "target": "python3 -m unittest"}},
    ]}
    runtime, run = start_and_approve(project, [
        calls(call("submit_plan", "p1", **plan)),
        calls(call("write_file", "w1", path="order.py", content=FIXED)),
        calls(call("Bash", "b1", command="python3 -m unittest", cwd=".")),
        calls(call("write_file", "w2", path="extra.py", content="X = 1\n")),
        calls(call("complete_task", "c1", task_id="T1", summary="done")),
        calls(call("Bash", "b2", command="python3 -m unittest", cwd=".")),
        calls(call("complete_task", "c2", task_id="T1", summary="done")),
        answer("ok"),
    ])
    runtime.advance(run)
    assert "is not complete" in tool_messages(run)["c1"]
    assert run["tasks"]["T1"]["evidence"]["call_id"] == "b2"


def test_done_tasks_are_not_repeated_after_resume(project):
    script = execution_script()
    runtime, run = start_and_approve(project, script, crash_at="after_execute", crash_on=2)
    with pytest.raises(Crash):
        runtime.advance(run)

    restarted = project.runtime([
        calls(call("Bash", "r1", command="python3 -m unittest -v", cwd=".")),
        calls(call("complete_task", "r2", task_id="T3", summary="tests pass")),
        answer("Done"),
    ])
    run = restarted.load(run["run_id"])
    assert [t["status"] for t in run["tasks"].values()] == ["done", "done", "running"]
    restarted.advance(run)
    restarted.resolve(run, "fail")
    restarted.advance(run)

    assert run["status"] == "finished"
    assert run["calls"]["t6"]["status"] == "failed"
    assert run["tasks"]["T3"]["evidence"]["call_id"] == "r1"
    assert run["calls"]["r1"]["task_id"] == "T3"
    task_messages = [m["content"].split(":")[0] for m in run["messages"] if m["role"] == "user" and "Current task" in m["content"]]
    assert task_messages == [f"The plan is approved. Current task {t}" for t in ("T1", "T2", "T3")]
    assert sum(1 for c in run["calls"].values() if c["task_id"] in ("T1", "T2")) == 5


def test_checklist_is_printed(project):
    runtime, run = start_and_approve(project, execution_script())
    runtime.advance(run)
    printed = "\n".join(project.output)
    assert "[~] T1 Read order.py\n[ ] T2 Fix order.py (after T1)\n[ ] T3 Run the tests (after T2)" in printed
    assert "[x] T1 Read order.py\n[x] T2 Fix order.py (after T1)\n[x] T3 Run the tests (after T2)" in printed
    assert checklist(run["tasks"]).startswith("[x] T1")
