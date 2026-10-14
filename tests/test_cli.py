from codeagent import cli

from helpers import FIXED, ORIGINAL, fix_script


def feed(monkeypatch, lines):
    answers = iter(lines)
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))


def test_interactive_session_confirms_actions_and_rolls_back(project, monkeypatch, capsys):
    runtime = project.runtime(fix_script())
    feed(monkeypatch, ["Fix the order total", "y", "y", "/diff", "/tasks", "/rollback", "/exit"])
    cli.chat(runtime, "safe", False)
    printed = capsys.readouterr().out

    assert "Confirmation required: write_file (mode safe)" in printed
    assert "+    return sum(prices) * (100 - discount_percent) / 100" in printed
    assert "Confirmation required: Bash (mode safe)" in printed
    assert "$ python3 -m unittest -v    (cwd: .)" in printed
    assert "Done: Fixed, the tests pass." in printed
    assert "Restored: order.py" in printed
    assert project.read("order.py") == ORIGINAL


def test_interactive_rejection_with_reason(project, monkeypatch, capsys):
    runtime = project.runtime(fix_script())
    feed(monkeypatch, ["Fix the order total", "n", "not allowed", "n", "", "/exit"])
    cli.chat(runtime, "safe", False)
    run = project.runtime().store.runs()[0]
    assert run["status"] == "finished"
    assert run["calls"]["c4"]["result"]["text"] == "error: rejected by the user: not allowed"
    assert run["calls"]["c5"]["result"]["text"] == "error: rejected by the user: no reason given"
    assert project.read("order.py") == ORIGINAL


def test_cli_views_work_after_restart(project, capsys):
    runtime = project.runtime(fix_script())
    run = runtime.advance(runtime.start("fix", mode="auto"))
    policy = str(project.root / "policy.toml")

    assert cli.main(["--policy", policy, "diff", run["run_id"]]) == 0
    assert cli.main(["--policy", policy, "status", run["run_id"]]) == 0
    assert project.read("order.py") == FIXED
    assert cli.main(["--policy", policy, "rollback", run["run_id"]]) == 0
    printed = capsys.readouterr().out
    assert "-    return sum(prices) - discount_percent" in printed
    assert "status: finished" in printed
    assert "Restored: order.py" in printed
    assert project.read("order.py") == ORIGINAL
