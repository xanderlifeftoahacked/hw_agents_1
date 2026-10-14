import os
import tomllib
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path

SHELL_SYNTAX = set("|&;<>()$`\\\"'*?[]{}~!#\n\r")

DEFAULT_BUDGET = {
    "model_requests": 40,
    "tool_calls": 80,
    "side_effects": 25,
    "result_bytes": 200_000,
}


class PolicyError(Exception):
    pass


@dataclass
class Policy:
    path: Path
    workspace: Path
    state_dir: Path
    allow_commands: list[str]
    deny_commands: list[list[str]]
    allow_dirs: list[Path]
    deny_dirs: list[Path]
    read_only: list[Path]
    generated: list[str]
    timeout: float
    max_output: int
    sandbox: str
    budget: dict

    def resolve(self, raw: str) -> Path:
        if not raw:
            raise PolicyError("empty path")
        path = Path(raw)
        if path.is_absolute():
            raise PolicyError(f"absolute paths are not allowed: {raw}")
        if ".." in path.parts:
            raise PolicyError(f"'..' is not allowed in paths: {raw}")
        current = self.workspace
        for part in path.parts:
            current = current / part
            if current.is_symlink():
                raise PolicyError(f"symlinks are not allowed: {raw}")
        real = current.resolve()
        self.check_location(real)
        return real

    def check_location(self, real: Path) -> None:
        if not real.is_relative_to(self.workspace):
            raise PolicyError(f"path is outside the workspace: {real}")
        if any(real.is_relative_to(d) for d in self.deny_dirs):
            raise PolicyError(f"directory is denied by policy: {self.relative(real)}")
        if not any(real.is_relative_to(d) for d in self.allow_dirs):
            raise PolicyError(f"directory is not allowed by policy: {self.relative(real)}")

    def allowed(self, real: Path) -> bool:
        try:
            self.check_location(real)
            return True
        except PolicyError:
            return False

    def check_writable(self, real: Path) -> None:
        if any(real.is_relative_to(p) for p in self.read_only):
            raise PolicyError(f"file is read-only by policy: {self.relative(real)}")

    def check_command(self, command: str, cwd: str) -> tuple[str, Path]:
        bad = sorted(set(command) & SHELL_SYNTAX)
        if bad:
            raise PolicyError(f"shell syntax is not allowed: {' '.join(repr(c) for c in bad)}")
        words = command.split()
        if not words:
            raise PolicyError("empty command")
        for denied in self.deny_commands:
            if any(words[i:i + len(denied)] == denied for i in range(len(words))):
                raise PolicyError(f"command is denied by policy: {' '.join(denied)}")
        for word in words:
            if word.startswith("/") or ".." in word.split("/"):
                raise PolicyError(f"paths outside the working directory are not allowed: {word}")
        normalized = " ".join(words)
        if normalized not in self.allow_commands:
            raise PolicyError(f"command is not in the allow list: {normalized}")
        directory = self.resolve(cwd)
        if not directory.is_dir():
            raise PolicyError(f"cwd is not a directory: {cwd}")
        return normalized, directory

    def is_generated(self, relative: Path) -> bool:
        return any(fnmatch(part, pattern) for part in relative.parts for pattern in self.generated)

    def walk(self, root: Path):
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames.sort()
            base = Path(dirpath)
            for name in sorted(filenames):
                path = base / name
                if not path.is_symlink() and not self.is_generated(path.relative_to(self.workspace)):
                    yield path

    def relative(self, real: Path) -> str:
        return real.relative_to(self.workspace).as_posix() if real.is_relative_to(self.workspace) else str(real)


def load_policy(path) -> Policy:
    path = Path(path).resolve()
    data = tomllib.loads(path.read_text())
    base = path.parent
    workspace = (base / data["workspace"]).resolve()
    state_dir = (base / data.get("state_dir", ".agent")).resolve()
    if not workspace.is_dir():
        raise PolicyError(f"workspace does not exist: {workspace}")
    if path.is_relative_to(workspace):
        raise PolicyError("policy file must be outside the workspace")
    if state_dir.is_relative_to(workspace):
        raise PolicyError("state directory must be outside the workspace")

    commands = data.get("commands", {})
    allow_commands = [" ".join(c.split()) for c in commands.get("allow", [])]
    for command in allow_commands:
        if set(command) & SHELL_SYNTAX:
            raise PolicyError(f"allowed command contains shell syntax: {command}")
    deny_commands = [c.split() for c in commands.get("deny", [])]

    directories = data.get("directories", {})
    allow_dirs = [(base / d).resolve() for d in directories.get("allow", [])]
    deny_dirs = [(base / d).resolve() for d in directories.get("deny", [])]
    for directory in allow_dirs + deny_dirs:
        if not directory.is_relative_to(workspace):
            raise PolicyError(f"directory must be inside the workspace: {directory}")

    read_only = [(base / p).resolve() for p in data.get("files", {}).get("read_only", [])]
    bash = data.get("bash", {})
    policy = Policy(
        path=path,
        workspace=workspace,
        state_dir=state_dir,
        allow_commands=allow_commands,
        deny_commands=deny_commands,
        allow_dirs=allow_dirs,
        deny_dirs=deny_dirs,
        read_only=read_only,
        generated=data.get("journal", {}).get("generated", []),
        timeout=float(bash.get("timeout_seconds", 30)),
        max_output=int(bash.get("max_output_bytes", 20_000)),
        sandbox=bash.get("sandbox", "none"),
        budget=DEFAULT_BUDGET | data.get("budget", {}),
    )
    if policy.sandbox not in ("none", "bwrap"):
        raise PolicyError(f"unknown sandbox: {policy.sandbox}")
    for source in workspace.rglob("*.py"):
        if policy.is_generated(source.relative_to(workspace)):
            raise PolicyError(f"generated patterns must not hide sources: {source}")
    return policy
