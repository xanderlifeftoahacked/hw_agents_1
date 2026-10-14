import pytest

from codeagent.llm import call
from codeagent.runtime import UserError

from helpers import FIXED, ORIGINAL, answer, calls, fix_script, tool_messages


def test_safe_mode_stops_and_continues_the_same_run(project):
    runtime = project.runtime(fix_script())
    run = runtime.advance(runtime.start("fix", mode="safe"))

    assert run["status"] == "waiting"
    assert run["waiting"] == {"kind": "confirm", "call_id": "c4"}
    assert project.read("order.py") == ORIGINAL
    assert set(tool_messages(run)) == {"c1", "c2", "c3"}

    restarted = project.runtime(project.model.replies)
    run = restarted.load(run["run_id"])
    restarted.approve(run)
    restarted.advance(run)
    assert project.read("order.py") == FIXED
    assert run["waiting"] == {"kind": "confirm", "call_id": "c5"}

    restarted.approve(run)
    restarted.advance(run)
    assert run["status"] == "finished"
    assert run["calls"]["c5"]["result"]["data"]["exit_code"] == 0


def test_rejected_action_returns_reason_to_the_model(project):
    runtime = project.runtime([
        calls(call("write_file", "w1", path="order.py", content="broken")),
        answer("ok, I will not change it"),
    ])
    run = runtime.advance(runtime.start("fix", mode="safe"))
    runtime.reject(run, "do not touch order.py")
    runtime.advance(run)

    assert run["status"] == "finished"
    assert project.read("order.py") == ORIGINAL
    assert tool_messages(run)["w1"] == "error: rejected by the user: do not touch order.py"
    assert run["calls"]["w1"]["status"] == "rejected"


def test_auto_mode_runs_write_and_bash_without_confirmation(project):
    runtime = project.runtime(fix_script())
    run = runtime.advance(runtime.start("fix", mode="auto"))
    assert run["status"] == "finished"
    assert all(c["decision"] is None for c in run["calls"].values())


def test_delete_needs_confirmation_even_in_auto_mode(project):
    project.file("tmp.txt").write_text("x")
    runtime = project.runtime([calls(call("delete_file", "d1", path="tmp.txt")), answer("deleted")])
    run = runtime.advance(runtime.start("cleanup", mode="auto"))
    assert run["waiting"] == {"kind": "confirm", "call_id": "d1"}
    assert project.file("tmp.txt").exists()

    runtime.approve(run)
    runtime.advance(run)
    assert run["status"] == "finished"
    assert not project.file("tmp.txt").exists()


def test_reads_do_not_need_confirmation_in_safe_mode(project):
    runtime = project.runtime([calls(call("read_file", "r1", path="order.py")), answer("read")])
    run = runtime.advance(runtime.start("read", mode="safe"))
    assert run["status"] == "finished"


def test_policy_is_checked_again_after_confirmation(project):
    runtime = project.runtime([
        calls(call("write_file", "w1", path="order.py", content=FIXED)),
        answer("policy changed"),
    ])
    run = runtime.advance(runtime.start("fix", mode="safe"))
    assert run["status"] == "waiting"

    policy_file = project.root / "policy.toml"
    policy_file.write_text(policy_file.read_text().replace(
        'read_only = ["workspace/test_order.py"]', 'read_only = ["workspace/test_order.py", "workspace/order.py"]'
    ))
    restarted = project.runtime(project.model.replies)
    run = restarted.load(run["run_id"])
    restarted.approve(run)
    restarted.advance(run)

    assert project.read("order.py") == ORIGINAL
    assert "read-only by policy" in tool_messages(run)["w1"]


def test_approve_requires_a_pending_decision(project):
    runtime = project.runtime([answer("nothing to do")])
    run = runtime.advance(runtime.start("hi", mode="safe"))
    with pytest.raises(UserError):
        runtime.approve(run)
