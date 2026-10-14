import os
import shutil

import pytest

from codeagent.llm import call
from codeagent.policy import PolicyError, load_policy

from helpers import ORIGINAL, answer, calls, sha, tool_messages


def test_allowed_command(project):
    command, cwd = project.policy().check_command("python3   -m unittest  -v", ".")
    assert command == "python3 -m unittest -v"
    assert cwd == project.workspace


def test_command_outside_allow_list_is_denied(project):
    with pytest.raises(PolicyError, match="not in the allow list"):
        project.policy().check_command("python3 -m pip install requests", ".")


def test_deny_has_priority_over_allow(project):
    with pytest.raises(PolicyError, match="denied by policy: rm"):
        project.policy().check_command("rm order.py", ".")


@pytest.mark.parametrize("command", [
    "ls | sh",
    "ls; rm -rf /",
    "ls && python3 -m unittest",
    "echo $(whoami)",
    "echo `id`",
    "ls > out.txt",
    "python3 -m unittest < input",
    "ls *",
    "ls ~",
])
def test_shell_syntax_is_denied(project, command):
    with pytest.raises(PolicyError, match="shell syntax"):
        project.policy().check_command(command, ".")


@pytest.mark.parametrize("command", ["bash -c ls", "python3 -c print"])
def test_nested_interpreters_are_denied(project, command):
    with pytest.raises(PolicyError, match="denied by policy"):
        project.policy().check_command(command, ".")


def test_allowed_command_in_denied_directory(project):
    with pytest.raises(PolicyError, match="directory is denied"):
        project.policy().check_command("ls", "private")


def test_cwd_outside_workspace_is_denied(project):
    with pytest.raises(PolicyError, match="'..'"):
        project.policy().check_command("ls", "..")


def test_file_tool_in_denied_subdirectory(project):
    runtime = project.runtime([
        calls(call("read_file", "p1", path="private/notes.txt"), call("list_files", "p2", path="private")),
        answer("denied"),
    ])
    run = runtime.advance(runtime.start("read private", mode="auto"))
    results = tool_messages(run)
    assert "directory is denied by policy: private" in results["p1"]
    assert "directory is denied by policy: private" in results["p2"]


@pytest.mark.parametrize("path", ["../policy.toml", "private/../../policy.toml", "/etc/passwd"])
def test_escape_by_dotdot_or_absolute_path(project, path):
    with pytest.raises(PolicyError):
        project.policy().resolve(path)


def test_escape_by_symlink(project, tmp_path):
    (tmp_path / "outside.txt").write_text("secret")
    os.symlink(tmp_path / "outside.txt", project.file("link.txt"))
    os.symlink(project.workspace / "private", project.file("private_link"))
    runtime = project.runtime([
        calls(
            call("read_file", "s1", path="link.txt"),
            call("read_file", "s2", path="private_link/notes.txt"),
            call("write_file", "s3", path="link.txt", content="x"),
        ),
        answer("denied"),
    ])
    run = runtime.advance(runtime.start("symlinks", mode="auto"))
    results = tool_messages(run)
    assert all("symlinks are not allowed" in results[c] for c in ("s1", "s2", "s3"))
    assert (tmp_path / "outside.txt").read_text() == "secret"


def test_policy_file_cannot_be_changed_by_tools(project):
    policy_file = project.root / "policy.toml"
    before = sha(policy_file)
    runtime = project.runtime([
        calls(
            call("write_file", "w1", path="../policy.toml", content="workspace = '/'"),
            call("delete_file", "w2", path="../policy.toml"),
        ),
        answer("denied"),
    ])
    run = runtime.advance(runtime.start("edit policy", mode="auto"))
    assert all("'..' is not allowed" in tool_messages(run)[c] for c in ("w1", "w2"))
    assert sha(policy_file) == before


def test_read_only_test_file(project):
    runtime = project.runtime([
        calls(call("write_file", "r1", path="test_order.py", content="")),
        answer("denied"),
    ])
    run = runtime.advance(runtime.start("edit tests", mode="auto"))
    assert "read-only by policy" in tool_messages(run)["r1"]
    assert "import unittest" in project.read("test_order.py")


def test_bash_denied_command_and_directory_are_not_executed(project):
    runtime = project.runtime([
        calls(
            call("Bash", "b1", command="rm order.py", cwd="."),
            call("Bash", "b2", command="ls", cwd="private"),
            call("Bash", "b3", command="cat order.py | sh", cwd="."),
        ),
        answer("denied"),
    ])
    run = runtime.advance(runtime.start("bash", mode="auto"))
    results = tool_messages(run)
    assert "command is denied by policy: rm" in results["b1"]
    assert "directory is denied by policy" in results["b2"]
    assert "shell syntax" in results["b3"]
    assert project.read("order.py") == ORIGINAL
    assert run["budget"]["used"]["side_effects"] == 0


def test_policy_must_live_outside_workspace(project):
    shutil.copy(project.root / "policy.toml", project.workspace / "policy.toml")
    text = (project.workspace / "policy.toml").read_text().replace('workspace = "workspace"', 'workspace = "."')
    (project.workspace / "policy.toml").write_text(text)
    with pytest.raises(PolicyError, match="outside the workspace"):
        load_policy(project.workspace / "policy.toml")


def test_allowed_directory_must_be_inside_workspace(project):
    path = project.root / "policy.toml"
    path.write_text(path.read_text().replace('allow = ["workspace"]', 'allow = ["."]'))
    with pytest.raises(PolicyError, match="inside the workspace"):
        load_policy(path)


def test_generated_patterns_cannot_hide_sources(project):
    path = project.root / "policy.toml"
    path.write_text(path.read_text().replace('"*.pyc"]', '"*.py"]'))
    with pytest.raises(PolicyError, match="must not hide sources"):
        load_policy(path)


def stdout(result) -> str:
    return result.text.split("--- stdout ---\n")[1].split("\n--- stderr ---")[0]


@pytest.mark.skipif(shutil.which("bwrap") is None, reason="bubblewrap is not installed")
def test_bwrap_sandbox_isolates_bash(project):
    from codeagent.tools import TOOLS

    policy = project.policy()
    policy.sandbox = "bwrap"
    bash = TOOLS["Bash"].run

    home = bash(policy, ("ls -A /home", project.workspace), {}, 10_000)
    assert stdout(home).strip() == ""

    network = bash(policy, ("cat /proc/net/dev", project.workspace), {}, 10_000)
    interfaces = [line.split(":")[0].strip() for line in stdout(network).splitlines() if ":" in line]
    assert interfaces == ["lo"]

    readonly = bash(policy, ("touch /usr/agent-test", project.workspace), {}, 10_000)
    assert readonly.data["exit_code"] != 0 and "Read-only file system" in readonly.text

    inside = bash(policy, ("touch created.txt", project.workspace), {}, 10_000)
    assert inside.data["exit_code"] == 0 and project.file("created.txt").exists()
