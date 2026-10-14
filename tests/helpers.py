import hashlib
import shutil
from pathlib import Path

from codeagent.llm import Reply, ScriptedModel, call
from codeagent.policy import load_policy
from codeagent.runtime import Runtime
from codeagent.store import Store

ROOT = Path(__file__).resolve().parent.parent

POLICY = """
workspace = "workspace"
state_dir = ".agent"

[commands]
allow = ["python3 -m unittest", "python3 -m unittest -v", "ls", "rm order.py", "sleep 5"]
deny = ["rm", "bash", "python3 -c"]

[directories]
allow = ["workspace"]
deny = ["workspace/private"]

[files]
read_only = ["workspace/test_order.py"]

[journal]
generated = ["__pycache__", "*.pyc"]

[bash]
timeout_seconds = {timeout}
max_output_bytes = 20000
sandbox = "none"

[budget]
model_requests = {model_requests}
tool_calls = {tool_calls}
side_effects = {side_effects}
result_bytes = {result_bytes}
"""

FIXED = '''def calculate_total(prices: list[float], discount_percent: float) -> float:
    if discount_percent < 0 or discount_percent > 100:
        raise ValueError("discount_percent must be between 0 and 100")
    return sum(prices) * (100 - discount_percent) / 100
'''

ORIGINAL = (ROOT / "workspace" / "order.py").read_text()

PLAN = {
    "goal": "Fix calculate_total so that the tests pass",
    "steps": [
        {"id": "T1", "description": "Read order.py", "tools": ["read_file"], "depends_on": [],
         "check": {"kind": "file_read", "target": "order.py"}},
        {"id": "T2", "description": "Fix order.py", "tools": ["write_file"], "depends_on": ["T1"],
         "check": {"kind": "file_changed", "target": "order.py"}},
        {"id": "T3", "description": "Run the tests", "tools": ["Bash"], "depends_on": ["T2"],
         "check": {"kind": "command_ok", "target": "python3 -m unittest -v"}},
    ],
    "risks": ["float rounding"],
    "assumptions": ["discount is a percent"],
    "done_criteria": "python3 -m unittest -v exits with code 0",
}


class Crash(BaseException):
    pass


def calls(*items) -> Reply:
    return Reply(tool_calls=list(items))


def answer(text: str) -> Reply:
    return Reply(content=text)


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fix_script() -> list[Reply]:
    return [
        calls(call("list_files", "c1", path=".")),
        calls(call("read_file", "c2", path="order.py"), call("read_file", "c3", path="test_order.py")),
        calls(call("write_file", "c4", path="order.py", content=FIXED)),
        calls(call("Bash", "c5", command="python3 -m unittest -v", cwd=".")),
        answer("Fixed, the tests pass."),
    ]


class Project:
    def __init__(self, root: Path, timeout: int = 20, **budget):
        self.root = root
        self.workspace = root / "workspace"
        self.workspace.mkdir()
        for name in ("order.py", "test_order.py"):
            shutil.copy(ROOT / "workspace" / name, self.workspace / name)
        (self.workspace / "private").mkdir()
        (self.workspace / "private" / "notes.txt").write_text("private notes\n")
        limits = {"model_requests": 40, "tool_calls": 80, "side_effects": 25, "result_bytes": 200_000} | budget
        (root / "policy.toml").write_text(POLICY.format(timeout=timeout, **limits))
        self.output = []
        self.model = None

    def policy(self):
        return load_policy(self.root / "policy.toml")

    def runtime(self, replies=(), crash_at=None, crash_on=1) -> Runtime:
        hits = []

        def fault(point):
            if point == crash_at:
                hits.append(point)
                if len(hits) == crash_on:
                    raise Crash(point)

        self.model = ScriptedModel(list(replies))
        policy = self.policy()
        return Runtime(policy, Store(policy.state_dir), self.model, out=self.output.append, fault=fault)

    def file(self, name: str) -> Path:
        return self.workspace / name

    def read(self, name: str) -> str:
        return self.file(name).read_text()


def tool_messages(run: dict) -> dict:
    return {m["tool_call_id"]: m["content"] for m in run["messages"] if m["role"] == "tool"}
