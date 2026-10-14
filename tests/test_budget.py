import pytest

from codeagent.llm import call

from helpers import FIXED, ORIGINAL, Crash, answer, calls, tool_messages


def reads(count: int):
    return [calls(call("list_files", f"l{i}", path=".")) for i in range(count)]


def test_model_requests_limit(make_project):
    project = make_project(model_requests=3)
    runtime = project.runtime(reads(10))
    run = runtime.advance(runtime.start("loop", mode="auto"))
    assert run["status"] == "failed"
    assert run["error"].startswith("budget exhausted: model_requests")
    assert run["budget"]["used"]["model_requests"] == 3
    assert len(project.model.requests) == 3


def test_tool_calls_limit_rejects_the_whole_batch(make_project):
    project = make_project(tool_calls=3)
    runtime = project.runtime([
        calls(call("read_file", "a1", path="order.py"), call("read_file", "a2", path="order.py")),
        calls(call("write_file", "b1", path="order.py", content=FIXED), call("list_files", "b2", path=".")),
    ])
    run = runtime.advance(runtime.start("fix", mode="auto"))
    assert run["status"] == "failed"
    assert "tool_calls" in run["error"]
    assert run["budget"]["used"]["tool_calls"] == 2
    assert "b1" not in run["calls"] and "b2" not in run["calls"]
    assert "not executed" in tool_messages(run)["b1"]
    assert project.read("order.py") == ORIGINAL


def test_side_effects_limit_rejects_the_whole_batch(make_project):
    project = make_project(side_effects=2)
    runtime = project.runtime([
        calls(
            call("write_file", "w1", path="a.txt", content="a"),
            call("write_file", "w2", path="b.txt", content="b"),
            call("write_file", "w3", path="c.txt", content="c"),
        ),
    ])
    run = runtime.advance(runtime.start("write", mode="auto"))
    assert run["status"] == "failed"
    assert "side_effects" in run["error"]
    assert not any(project.file(n).exists() for n in ("a.txt", "b.txt", "c.txt"))
    assert run["budget"]["used"]["side_effects"] == 0


def test_side_effects_limit_is_exhausted(make_project):
    project = make_project(side_effects=1)
    runtime = project.runtime([
        calls(call("write_file", "w1", path="order.py", content=FIXED)),
        calls(call("Bash", "b1", command="python3 -m unittest", cwd=".")),
    ])
    run = runtime.advance(runtime.start("fix", mode="auto"))
    assert run["status"] == "failed"
    assert run["budget"]["used"]["side_effects"] == 1
    assert "b1" not in run["calls"]


def test_result_bytes_are_truncated_then_exhausted(make_project):
    project = make_project(result_bytes=100)
    runtime = project.runtime([
        calls(call("read_file", "r1", path="test_order.py")),
        calls(call("read_file", "r2", path="order.py")),
    ])
    run = runtime.advance(runtime.start("read", mode="auto"))
    first = run["calls"]["r1"]["result"]
    assert first["truncated"] is True
    assert "[truncated: output was cut to 100 bytes, the result is incomplete]" in first["text"]
    assert first["text"].startswith("import unittest")
    assert run["budget"]["used"]["result_bytes"] == 100
    assert run["status"] == "failed"
    assert "result_bytes" in run["error"]
    assert run["calls"]["r2"]["status"] == "proposed"


def test_bash_output_gets_the_remaining_budget(make_project):
    project = make_project(result_bytes=60)
    runtime = project.runtime([calls(call("Bash", "b1", command="python3 -m unittest -v", cwd=".")), answer("cut")])
    run = runtime.advance(runtime.start("tests", mode="auto"))
    result = run["calls"]["b1"]["result"]
    assert result["truncated"] is True
    assert result["text"].startswith("exit_code: 1")
    assert result["data"]["exit_code"] == 1
    assert run["budget"]["used"]["result_bytes"] == 60


def test_counters_survive_resume_and_plan_approval(project):
    runtime = project.runtime([
        calls(call("read_file", "p1", path="order.py")),
        calls(call("submit_plan", "p2", goal="g", steps=[
            {"id": "T1", "description": "fix", "tools": ["write_file"], "depends_on": [],
             "check": {"kind": "file_changed", "target": "order.py"}},
        ], risks=[], assumptions=[], done_criteria="fixed")),
    ])
    run = runtime.advance(runtime.start("fix", mode="safe", plan=True))
    used = dict(run["budget"]["used"])
    assert used["model_requests"] == 2 and used["tool_calls"] == 2

    restarted = project.runtime([calls(call("write_file", "w1", path="order.py", content=FIXED))])
    run = restarted.load(run["run_id"])
    assert run["budget"]["used"] == used
    restarted.approve(run)
    restarted.advance(run)
    assert run["budget"]["used"]["model_requests"] == 3
    assert run["budget"]["used"]["tool_calls"] == 3

    again = project.runtime([answer("x")])
    run = again.load(run["run_id"])
    assert run["budget"]["used"]["model_requests"] == 3
    assert run["waiting"]["kind"] == "confirm"


def test_spent_budget_is_kept_after_a_crash(make_project):
    project = make_project(side_effects=2)
    runtime = project.runtime([calls(call("write_file", "w1", path="order.py", content=FIXED))], crash_at="after_execute")
    run = runtime.start("fix", mode="auto")
    with pytest.raises(Crash):
        runtime.advance(run)

    restarted = project.runtime([answer("done")])
    run = restarted.load(run["run_id"])
    assert run["budget"]["used"]["side_effects"] == 1
    assert run["budget"]["used"]["result_bytes"] == 0
    restarted.advance(run)
    restarted.resolve(run, "retry", changes="ours")
    restarted.advance(run)
    assert run["status"] == "finished"
    assert run["budget"]["used"]["side_effects"] == 2


def test_action_does_not_start_when_the_limit_is_spent(make_project):
    project = make_project(side_effects=1)
    runtime = project.runtime([calls(call("write_file", "w1", path="order.py", content=FIXED))], crash_at="after_execute")
    run = runtime.start("fix", mode="auto")
    with pytest.raises(Crash):
        runtime.advance(run)

    restarted = project.runtime([])
    run = restarted.advance(restarted.load(run["run_id"]))
    restarted.resolve(run, "retry", changes="foreign")
    restarted.advance(run)
    assert run["status"] == "failed"
    assert "side_effects" in run["error"]
    assert [a["status"] for a in run["calls"]["w1"]["attempts"]] == ["uncertain"]
