import os
import signal
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .policy import Policy

READ = "read"
WRITE = "write"
EXEC = "exec"
DELETE = "delete"
CONTROL = "control"
SIDE_EFFECTS = {WRITE, EXEC, DELETE}

PLANNING = "planning"
EXECUTING = "executing"


class ToolError(Exception):
    pass


@dataclass
class Result:
    text: str
    data: dict = field(default_factory=dict)


@dataclass
class Tool:
    name: str
    description: str
    schema: dict
    kind: str
    phases: tuple
    retryable: bool
    check: Callable | None = None
    run: Callable | None = None

    def spec(self) -> dict:
        return {
            "type": "function",
            "function": {"name": self.name, "description": self.description, "parameters": self.schema},
        }


def schema(required: list[str], **properties) -> dict:
    return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}


def text(description: str) -> dict:
    return {"type": "string", "description": description}


def check_read(policy: Policy, args: dict) -> Path:
    return policy.resolve(args.get("path", "."))


def check_write(policy: Policy, args: dict) -> Path:
    path = policy.resolve(args["path"])
    policy.check_writable(path)
    return path


def check_bash(policy: Policy, args: dict) -> tuple[str, Path]:
    return policy.check_command(args["command"], args["cwd"])


def list_files(policy: Policy, root: Path, args: dict, limit: int) -> Result:
    if not root.is_dir():
        raise ToolError(f"not a directory: {args.get('path', '.')}")
    files = [policy.relative(p) for p in policy.walk(root) if policy.allowed(p)]
    return Result("\n".join(files) or "(no files)")


def read_file(policy: Policy, path: Path, args: dict, limit: int) -> Result:
    if not path.is_file():
        raise ToolError(f"file not found: {args['path']}")
    with path.open("rb") as file:
        return Result(file.read(limit + 1).decode("utf-8", errors="replace"))


def write_file(policy: Policy, path: Path, args: dict, limit: int) -> Result:
    if path.is_dir():
        raise ToolError(f"is a directory: {args['path']}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(args["content"])
    return Result(f"wrote {len(args['content'].encode())} bytes to {args['path']}")


def edit_file(policy: Policy, path: Path, args: dict, limit: int) -> Result:
    if not path.is_file():
        raise ToolError(f"file not found: {args['path']}")
    content = path.read_text()
    count = content.count(args["old"])
    if count != 1:
        raise ToolError(f"'old' must occur exactly once in {args['path']}, found {count}")
    path.write_text(content.replace(args["old"], args["new"]))
    return Result(f"edited {args['path']}")


def delete_file(policy: Policy, path: Path, args: dict, limit: int) -> Result:
    if not path.is_file():
        raise ToolError(f"file not found: {args['path']}")
    path.unlink()
    return Result(f"deleted {args['path']}")


def sandbox_prefix(workspace: Path, cwd: Path) -> list[str]:
    return [
        "bwrap",
        "--ro-bind", "/", "/",
        "--tmpfs", "/home",
        "--tmpfs", "/root",
        "--tmpfs", "/run",
        "--tmpfs", "/tmp",
        "--dev", "/dev",
        "--proc", "/proc",
        "--bind", str(workspace), str(workspace),
        "--unshare-all",
        "--die-with-parent",
        "--new-session",
        "--chdir", str(cwd),
    ]


def bash(policy: Policy, prepared: tuple[str, Path], args: dict, limit: int) -> Result:
    command, cwd = prepared
    argv = ["bash", "--noprofile", "--norc", "-c", command]
    if policy.sandbox == "bwrap":
        argv = sandbox_prefix(policy.workspace, cwd) + argv
    env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "HOME": str(cwd),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        process = subprocess.Popen(
            argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=out, stderr=err, start_new_session=True
        )
        timed_out = False
        try:
            code = process.wait(timeout=policy.timeout)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            code = process.wait()
            timed_out = True
        out.seek(0)
        err.seek(0)
        stdout = out.read(limit + 1).decode("utf-8", errors="replace")
        stderr = err.read(limit + 1).decode("utf-8", errors="replace")
    header = f"exit_code: {code}" + (f" (timed out after {policy.timeout:g}s)" if timed_out else "")
    output = f"{header}\n--- stdout ---\n{stdout}\n--- stderr ---\n{stderr}"
    return Result(output, {"exit_code": code, "timed_out": timed_out})


BOTH = (PLANNING, EXECUTING)
ONLY_EXECUTING = (EXECUTING,)

TOOLS = {
    tool.name: tool
    for tool in [
        Tool(
            "list_files",
            "List all files under a workspace directory recursively.",
            schema([], path=text("Directory relative to the workspace root, default '.'")),
            READ, BOTH, True, check_read, list_files,
        ),
        Tool(
            "read_file",
            "Read a text file from the workspace.",
            schema(["path"], path=text("File path relative to the workspace root")),
            READ, BOTH, True, check_read, read_file,
        ),
        Tool(
            "write_file",
            "Create a file or overwrite it with the full new content.",
            schema(["path", "content"], path=text("File path relative to the workspace root"), content=text("Full file content")),
            WRITE, ONLY_EXECUTING, True, check_write, write_file,
        ),
        Tool(
            "edit_file",
            "Replace exactly one occurrence of 'old' with 'new' in a file.",
            schema(["path", "old", "new"], path=text("File path relative to the workspace root"), old=text("Exact text to replace"), new=text("Replacement text")),
            WRITE, ONLY_EXECUTING, False, check_write, edit_file,
        ),
        Tool(
            "delete_file",
            "Delete a file from the workspace.",
            schema(["path"], path=text("File path relative to the workspace root")),
            DELETE, ONLY_EXECUTING, True, check_write, delete_file,
        ),
        Tool(
            "Bash",
            "Run one allowed command with bash in a workspace directory. Returns exit code, stdout and stderr.",
            schema(["command", "cwd"], command=text("Exact allowed command"), cwd=text("Working directory relative to the workspace root, usually '.'")),
            EXEC, ONLY_EXECUTING, False, check_bash, bash,
        ),
    ]
}
