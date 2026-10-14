import hashlib

import pytest

from codeagent import journal
from codeagent.journal import RollbackConflict
from codeagent.llm import call
from codeagent.runtime import UserError

from helpers import FIXED, ORIGINAL, Crash, answer, calls, fix_script, sha


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def finished_fix(project):
    project.file("README.md").write_text("not touched by the agent\n")
    runtime = project.runtime(fix_script())
    run = runtime.advance(runtime.start("fix", mode="auto"))
    assert run["status"] == "finished"
    return run["run_id"]


def test_journal_keeps_before_after_and_diff(project):
    run_id = finished_fix(project)
    runtime = project.runtime()
    run = runtime.load(run_id)

    assert run["journal"]["files"] == {"order.py": {"before": digest(ORIGINAL), "after": digest(FIXED)}}
    change = run["journal"]["changes"][0]
    assert change["call_id"] == "c4" and change["paths"] == ["order.py"]
    text = journal.net_diff(runtime.store, run)
    assert "--- a/order.py" in text and "+++ b/order.py" in text
    assert "-    return sum(prices) - discount_percent" in text
    assert "+    return sum(prices) * (100 - discount_percent) / 100" in text


def test_rollback_after_restart_restores_only_changed_files(project):
    run_id = finished_fix(project)
    project.file("user_file.txt").write_text("created by the user after the run\n")
    tests_hash = sha(project.file("test_order.py"))

    runtime = project.runtime()
    run = runtime.load(run_id)
    assert runtime.rollback(run) == ["order.py"]

    assert project.read("order.py") == ORIGINAL
    assert project.read("README.md") == "not touched by the agent\n"
    assert project.read("user_file.txt") == "created by the user after the run\n"
    assert sha(project.file("test_order.py")) == tests_hash
    assert runtime.load(run_id)["status"] == "rolled_back"


def test_rollback_conflict_changes_nothing(project):
    runtime = project.runtime([
        calls(call("write_file", "w1", path="order.py", content=FIXED), call("write_file", "w2", path="helper.py", content="X = 1\n")),
        answer("done"),
    ])
    run = runtime.advance(runtime.start("fix", mode="auto"))
    project.file("order.py").write_text(FIXED + "# edited by a human\n")

    restarted = project.runtime()
    run = restarted.load(run["run_id"])
    with pytest.raises(RollbackConflict) as error:
        restarted.rollback(run)
    assert error.value.problems == ["order.py: changed outside the agent after its last action"]
    assert project.read("order.py") == FIXED + "# edited by a human\n"
    assert project.read("helper.py") == "X = 1\n"
    assert restarted.load(run["run_id"])["status"] == "finished"


def test_rollback_removes_created_and_restores_deleted_files(project):
    project.file("old.txt").write_text("old\n")
    runtime = project.runtime([
        calls(call("write_file", "w1", path="pkg/new.py", content="NEW = 1\n"), call("delete_file", "d1", path="old.txt")),
        answer("done"),
    ])
    run = runtime.advance(runtime.start("files", mode="auto"))
    runtime.approve(run)
    runtime.advance(run)
    assert run["journal"]["files"]["pkg/new.py"]["before"] is None
    assert run["journal"]["files"]["old.txt"]["after"] is None

    runtime.rollback(run)
    assert not project.file("pkg/new.py").exists()
    assert project.read("old.txt") == "old\n"


def test_external_change_between_actions_is_a_conflict(project):
    runtime = project.runtime([
        calls(call("write_file", "w1", path="order.py", content=FIXED)),
        calls(call("write_file", "w2", path="notes.txt", content="agent notes\n")),
        answer("done"),
    ])
    run = runtime.advance(runtime.start("fix", mode="safe"))
    runtime.approve(run)
    runtime.advance(run)
    assert run["waiting"]["call_id"] == "w2"

    project.file("order.py").write_text("human version\n")
    runtime.approve(run)
    runtime.advance(run)
    assert run["waiting"]["kind"] == "recovery"
    assert run["waiting"]["reason"] == "conflict"
    assert run["waiting"]["changes"] == ["order.py"]
    assert not project.file("notes.txt").exists()

    runtime.resolve(run, "retry", changes="foreign")
    runtime.advance(run)
    assert run["status"] == "finished"
    assert project.read("notes.txt") == "agent notes\n"
    with pytest.raises(RollbackConflict):
        runtime.rollback(run)
    assert project.read("notes.txt") == "agent notes\n"


def test_uncertain_call_blocks_rollback_until_resolved(project):
    runtime = project.runtime([calls(call("write_file", "w1", path="order.py", content=FIXED))], crash_at="after_execute")
    run = runtime.start("fix", mode="auto")
    with pytest.raises(Crash):
        runtime.advance(run)

    restarted = project.runtime([answer("done")])
    run = restarted.load(run["run_id"])
    with pytest.raises(UserError, match="resolve the pending recovery"):
        restarted.rollback(run)
    assert project.read("order.py") == FIXED
    assert run["waiting"]["changes"] == ["order.py"]

    restarted.resolve(run, "fail", changes="ours")
    restarted.advance(run)
    assert run["journal"]["files"]["order.py"]["after"] == digest(FIXED)
    restarted.rollback(run)
    assert project.read("order.py") == ORIGINAL


def test_generated_files_are_excluded_from_the_journal(project):
    (project.workspace / "__pycache__").mkdir()
    (project.workspace / "__pycache__" / "order.cpython-314.pyc").write_bytes(b"\x00")
    runtime = project.runtime(fix_script())
    run = runtime.advance(runtime.start("fix", mode="auto"))
    assert not any("__pycache__" in path for path in run["journal"]["tree"])
    assert "order.py" in run["journal"]["tree"]
