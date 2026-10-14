from codeagent.llm import call
from codeagent.runtime import ALL_TOOLS

from helpers import FIXED, answer, calls, fix_script, sha, tool_messages


def test_control_task_is_fixed_and_verified_with_bash(project):
    test_hash = sha(project.file("test_order.py"))
    runtime = project.runtime(fix_script())
    run = runtime.advance(runtime.start("fix the order total", mode="auto"))

    assert run["status"] == "finished"
    assert project.read("order.py") == FIXED
    assert sha(project.file("test_order.py")) == test_hash
    bash = run["calls"]["c5"]
    assert bash["name"] == "Bash"
    assert bash["status"] == "completed"
    assert bash["result"]["data"]["exit_code"] == 0
    assert "OK" in bash["result"]["text"]


def test_tool_results_use_role_tool_and_original_ids(project):
    runtime = project.runtime(fix_script())
    run = runtime.advance(runtime.start("fix", mode="auto"))

    results = tool_messages(run)
    assert set(results) == {"c1", "c2", "c3", "c4", "c5"}
    third_request = project.model.requests[2]["messages"]
    assert [m["tool_call_id"] for m in third_request if m["role"] == "tool"][-2:] == ["c2", "c3"]
    assert "calculate_total" in results["c2"]
    assert "import unittest" in results["c3"]


def test_list_files_hides_denied_directory(project):
    runtime = project.runtime(fix_script())
    run = runtime.advance(runtime.start("fix", mode="auto"))
    assert tool_messages(run)["c1"].splitlines() == ["order.py", "test_order.py"]


def test_bash_reports_failing_tests_with_exit_code(project):
    runtime = project.runtime([
        calls(call("Bash", "b1", command="python3 -m unittest", cwd=".")),
        answer("tests fail"),
    ])
    run = runtime.advance(runtime.start("run tests", mode="auto"))
    result = run["calls"]["b1"]["result"]
    assert result["data"]["exit_code"] == 1
    assert "FAILED" in result["text"]
    assert "--- stderr ---" in result["text"]


def test_tool_errors_are_returned_to_the_model(project):
    runtime = project.runtime([
        calls(
            call("no_such_tool", "e1"),
            {"id": "e2", "name": "read_file", "arguments": "{not json"},
            call("read_file", "e3"),
            call("read_file", "e4", path="missing.py"),
            call("write_file", "e5", path="order.py"),
        ),
        answer("done"),
    ])
    run = runtime.advance(runtime.start("break things", mode="auto"))

    results = tool_messages(run)
    assert run["status"] == "finished"
    assert "unknown tool" in results["e1"]
    assert "not valid JSON" in results["e2"]
    assert "invalid arguments" in results["e3"] and "'path' is a required property" in results["e3"]
    assert "file not found" in results["e4"]
    assert "'content' is a required property" in results["e5"]


def test_bash_timeout(make_project):
    project = make_project(timeout=1)
    runtime = project.runtime([calls(call("Bash", "s1", command="sleep 5", cwd=".")), answer("slow")])
    run = runtime.advance(runtime.start("sleep", mode="auto"))
    result = run["calls"]["s1"]["result"]
    assert result["data"]["timed_out"] is True
    assert "timed out" in result["text"]


def test_tools_are_described_with_json_schema(project):
    runtime = project.runtime([answer("ok")])
    run = runtime.advance(runtime.start("hi", mode="auto"))
    assert run["status"] == "finished"
    specs = {spec["function"]["name"]: spec["function"] for spec in runtime.specs(run)}
    assert {"list_files", "read_file", "write_file", "edit_file", "delete_file", "Bash"} <= set(specs)
    assert specs["Bash"]["parameters"]["required"] == ["command", "cwd"]
    assert all(spec["description"] for spec in specs.values())
    assert ALL_TOOLS["Bash"].retryable is False
