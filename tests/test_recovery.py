import pytest

from codeagent.llm import call
from codeagent.runtime import UserError

from helpers import FIXED, ORIGINAL, Crash, answer, calls, tool_messages


def crash_on_write(project, script, crash_on=1):
    runtime = project.runtime(script, crash_at="after_execute", crash_on=crash_on)
    run = runtime.start("fix", mode="auto")
    with pytest.raises(Crash):
        runtime.advance(run)
    return run["run_id"]


def test_crash_between_execution_and_result_makes_the_call_uncertain(project):
    run_id = crash_on_write(project, [
        calls(call("write_file", "w1", path="order.py", content=FIXED)),
        answer("done"),
    ])
    runtime = project.runtime([answer("done")])
    run = runtime.load(run_id)
    assert run["calls"]["w1"]["status"] == "started"

    runtime.advance(run)
    assert run["status"] == "waiting"
    assert run["waiting"]["kind"] == "recovery"
    assert run["waiting"]["reason"] == "uncertain"
    assert run["waiting"]["changes"] == ["order.py"]
    assert "+    if discount_percent < 0" in run["waiting"]["diff"]
    assert run["calls"]["w1"]["attempts"] == [{"n": 1, "status": "uncertain"}]
    assert project.model.requests == []


def test_retry_is_a_new_attempt_of_the_same_call(project):
    run_id = crash_on_write(project, [calls(call("write_file", "w1", path="order.py", content=FIXED))])
    runtime = project.runtime([answer("done")])
    run = runtime.advance(runtime.load(run_id))

    with pytest.raises(UserError, match="--changes"):
        runtime.resolve(run, "retry")
    runtime.resolve(run, "retry", changes="ours")
    runtime.advance(run)

    assert run["status"] == "finished"
    assert [a["status"] for a in run["calls"]["w1"]["attempts"]] == ["uncertain", "completed"]
    assert run["calls"]["w1"]["status"] == "completed"
    assert project.read("order.py") == FIXED


def test_bash_is_not_safely_retryable(project):
    run_id = crash_on_write(project, [
        calls(call("Bash", "b1", command="python3 -m unittest", cwd=".")),
        answer("tests status unknown"),
    ])
    runtime = project.runtime([answer("tests status unknown")])
    run = runtime.advance(runtime.load(run_id))
    assert run["waiting"]["changes"] == []

    with pytest.raises(UserError, match="not safely retryable"):
        runtime.resolve(run, "retry")
    runtime.resolve(run, "fail")
    runtime.advance(run)

    assert run["status"] == "finished"
    assert "outcome is unknown after a crash" in tool_messages(run)["b1"]
    assert len(run["calls"]["b1"]["attempts"]) == 1


def test_abort_finishes_the_run_with_error(project):
    run_id = crash_on_write(project, [calls(call("write_file", "w1", path="order.py", content=FIXED))])
    runtime = project.runtime([])
    run = runtime.advance(runtime.load(run_id))
    runtime.resolve(run, "abort", changes="foreign")
    runtime.advance(run)
    assert run["status"] == "failed"
    assert "aborted by the user" in run["error"]


def test_completed_actions_are_not_repeated_after_resume(project):
    run_id = crash_on_write(project, [
        calls(call("write_file", "w1", path="notes.txt", content="first\n")),
        calls(call("Bash", "b1", command="python3 -m unittest", cwd=".")),
    ], crash_on=2)
    runtime = project.runtime([answer("finished")])
    run = runtime.load(run_id)
    assert run["calls"]["w1"]["status"] == "completed"
    assert run["calls"]["b1"]["status"] == "started"

    runtime.advance(run)
    runtime.resolve(run, "fail")
    runtime.advance(run)

    assert run["status"] == "finished"
    assert len(run["calls"]["w1"]["attempts"]) == 1
    assert run["budget"]["used"]["side_effects"] == 2


def test_repeated_tool_call_id_is_not_executed_again(project):
    runtime = project.runtime([
        calls(call("write_file", "w1", path="order.py", content=FIXED)),
        calls(call("write_file", "w1", path="order.py", content=FIXED)),
        answer("done"),
    ])
    run = runtime.advance(runtime.start("fix", mode="auto"))
    assert run["status"] == "finished"
    assert len(run["calls"]["w1"]["attempts"]) == 1
    assert run["budget"]["used"]["side_effects"] == 1
    replies = [m["content"] for m in run["messages"] if m["role"] == "tool"]
    assert replies[0] == replies[1] == f"wrote {len(FIXED)} bytes to order.py"


def test_repeated_tool_call_id_with_other_arguments_is_a_protocol_error(project):
    runtime = project.runtime([
        calls(call("write_file", "w1", path="order.py", content=FIXED)),
        calls(call("write_file", "w1", path="order.py", content="broken")),
        answer("done"),
    ])
    run = runtime.advance(runtime.start("fix", mode="auto"))
    replies = [m["content"] for m in run["messages"] if m["role"] == "tool"]
    assert "protocol error" in replies[1]
    assert project.read("order.py") == FIXED
    assert run["budget"]["used"]["side_effects"] == 1


def test_crash_during_model_request_is_resumed(project):
    runtime = project.runtime([])
    run = runtime.start("fix", mode="auto")
    with pytest.raises(RuntimeError):
        runtime.advance(run)
    restarted = project.runtime([answer("done")])
    run = restarted.advance(restarted.load(run["run_id"]))
    assert run["status"] == "finished"
    assert run["budget"]["used"]["model_requests"] == 2
    assert project.read("order.py") == ORIGINAL
