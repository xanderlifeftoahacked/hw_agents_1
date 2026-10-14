import copy

from codeagent.llm import call

from helpers import FIXED, ORIGINAL, PLAN, answer, calls, sha, tool_messages


def planning_script(plan=PLAN):
    return [
        calls(call("list_files", "p1", path="."), call("read_file", "p2", path="order.py")),
        calls(
            call("write_file", "p3", path="order.py", content=FIXED),
            call("Bash", "p4", command="python3 -m unittest", cwd="."),
            call("delete_file", "p5", path="order.py"),
            call("edit_file", "p6", path="order.py", old="-", new="*"),
        ),
        answer("I think the plan is clear"),
        calls(call("submit_plan", "p7", **plan)),
    ]


def test_no_side_effects_before_plan_approval(project):
    before = {name: sha(project.file(name)) for name in ("order.py", "test_order.py", "private/notes.txt")}
    runtime = project.runtime(planning_script())
    run = runtime.advance(runtime.start("fix", mode="auto", plan=True))

    assert run["status"] == "waiting"
    assert run["waiting"] == {"kind": "plan", "call_id": "p7"}
    assert {name: sha(project.file(name)) for name in before} == before
    assert sorted(p.relative_to(project.workspace).as_posix() for p in project.workspace.rglob("*")) == [
        "order.py", "private", "private/notes.txt", "test_order.py",
    ]
    assert run["budget"]["used"]["side_effects"] == 0
    assert run["journal"] == {"tree": None, "files": {}, "changes": [], "rolled_back": False}
    assert all(c["attempts"] == [] for c in run["calls"].values())
    results = tool_messages(run)
    for call_id in ("p3", "p4", "p5", "p6"):
        assert "not available in the planning phase" in results[call_id]
        assert run["calls"][call_id]["status"] == "denied"


def test_planning_offers_only_read_tools(project):
    runtime = project.runtime(planning_script())
    runtime.advance(runtime.start("fix", mode="auto", plan=True))
    assert project.model.requests[0]["tools"] == ["list_files", "read_file", "submit_plan"]


def test_plan_is_stored_as_structured_data_with_run_id(project):
    runtime = project.runtime(planning_script())
    run = runtime.advance(runtime.start("fix", mode="auto", plan=True))
    stored = project.runtime().load(run["run_id"])["plan"]
    assert stored["status"] == "proposed"
    assert stored["goal"] == PLAN["goal"]
    assert [s["id"] for s in stored["steps"]] == ["T1", "T2", "T3"]
    assert stored["steps"][2]["tools"] == ["Bash"]
    assert stored["risks"] and stored["assumptions"] and stored["done_criteria"]
    assert all(c["phase"] == "planning" and c["task_id"] is None for c in run["calls"].values())


def test_invalid_plan_is_returned_to_the_model(project):
    broken = copy.deepcopy(PLAN)
    broken["steps"][1]["depends_on"] = ["T9"]
    forbidden = copy.deepcopy(PLAN)
    forbidden["steps"][2]["check"]["target"] = "curl http://example.com"
    command_as_tool = copy.deepcopy(PLAN)
    command_as_tool["steps"][2]["tools"] = ["python3 -m unittest"]
    runtime = project.runtime([
        calls(call("submit_plan", "s1", **broken)),
        calls(call("submit_plan", "s2", **forbidden)),
        calls(call("submit_plan", "s3", goal="no steps")),
        calls(call("submit_plan", "s4", **command_as_tool)),
        calls(call("submit_plan", "s5", **PLAN)),
    ])
    run = runtime.advance(runtime.start("fix", mode="auto", plan=True))
    results = tool_messages(run)
    assert "depends on T9" in results["s1"]
    assert "policy does not allow" in results["s2"]
    assert "invalid arguments" in results["s3"]
    assert "'python3 -m unittest' is not one of" in results["s4"]
    assert run["waiting"] == {"kind": "plan", "call_id": "s5"}


def test_rejected_plan_goes_back_to_the_model(project):
    runtime = project.runtime([calls(call("submit_plan", "s1", **PLAN)), calls(call("submit_plan", "s2", **PLAN))])
    run = runtime.advance(runtime.start("fix", mode="auto", plan=True))
    runtime.reject(run, "add a step for the edge cases")
    runtime.advance(run)
    assert "Plan rejected by the user: add a step for the edge cases" in tool_messages(run)["s1"]
    assert run["waiting"] == {"kind": "plan", "call_id": "s2"}
    assert run["phase"] == "planning"


def test_approved_plan_still_requires_confirmations_in_safe_mode(project):
    runtime = project.runtime([
        calls(call("submit_plan", "s1", **PLAN)),
        calls(call("read_file", "e1", path="order.py")),
        calls(call("complete_task", "e2", task_id="T1", summary="read")),
        calls(call("write_file", "e3", path="order.py", content=FIXED)),
    ])
    run = runtime.advance(runtime.start("fix", mode="safe", plan=True))
    runtime.approve(run)
    runtime.advance(run)

    assert run["phase"] == "executing"
    assert run["waiting"] == {"kind": "confirm", "call_id": "e3"}
    assert project.read("order.py") == ORIGINAL
