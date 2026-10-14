import difflib
import hashlib
from pathlib import Path

from .policy import Policy, PolicyError
from .store import Store


class RollbackConflict(Exception):
    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


def new() -> dict:
    return {"tree": None, "files": {}, "changes": [], "rolled_back": False}


def file_hash(path: Path) -> str | None:
    if not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def snapshot(store: Store, run: dict, policy: Policy) -> dict:
    return {
        policy.relative(path): store.put_blob(run["run_id"], path.read_bytes())
        for path in policy.walk(policy.workspace)
    }


def changed(before: dict, after: dict) -> list[str]:
    return sorted(p for p in before.keys() | after.keys() if before.get(p) != after.get(p))


def content(store: Store, run_id: str, digest: str | None) -> str:
    if digest is None:
        return ""
    return store.blob(run_id, digest).decode("utf-8", errors="replace")


def diff(store: Store, run_id: str, before: dict, after: dict, paths: list[str]) -> str:
    lines = []
    for path in paths:
        old, new_ = before.get(path), after.get(path)
        lines.extend(
            difflib.unified_diff(
                content(store, run_id, old).splitlines(keepends=True),
                content(store, run_id, new_).splitlines(keepends=True),
                fromfile=f"a/{path}" if old else "/dev/null",
                tofile=f"b/{path}" if new_ else "/dev/null",
            )
        )
    return "".join(line if line.endswith("\n") else line + "\n" for line in lines)


def record(store: Store, run: dict, before: dict, after: dict, call_id: str, attempt: int, task_id: str | None) -> list[str]:
    journal = run["journal"]
    paths = changed(before, after)
    if paths:
        for path in paths:
            entry = journal["files"].setdefault(path, {"before": before.get(path)})
            entry["after"] = after.get(path)
        run["seq"] += 1
        journal["changes"].append({
            "seq": run["seq"],
            "call_id": call_id,
            "attempt": attempt,
            "task_id": task_id,
            "paths": paths,
            "diff": diff(store, run["run_id"], before, after, paths),
        })
    journal["tree"] = after
    return paths


def net_diff(store: Store, run: dict) -> str:
    files = run["journal"]["files"]
    before = {p: e["before"] for p, e in files.items() if e["before"]}
    after = {p: e["after"] for p, e in files.items() if e["after"]}
    return diff(store, run["run_id"], before, after, sorted(files))


def check_rollback(run: dict, policy: Policy) -> list[tuple[Path, dict]]:
    problems, targets = [], []
    for relative, entry in run["journal"]["files"].items():
        try:
            path = policy.resolve(relative)
        except PolicyError as error:
            problems.append(f"{relative}: {error}")
            continue
        if file_hash(path) != entry["after"]:
            problems.append(f"{relative}: changed outside the agent after its last action")
        targets.append((path, entry))
    if problems:
        raise RollbackConflict(problems)
    return targets


def rollback(store: Store, run: dict, policy: Policy) -> list[str]:
    journal = run["journal"]
    if journal["rolled_back"]:
        raise RollbackConflict(["run is already rolled back"])
    targets = check_rollback(run, policy)
    for path, entry in targets:
        if entry["before"] is None:
            path.unlink(missing_ok=True)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(store.blob(run["run_id"], entry["before"]))
    journal["rolled_back"] = True
    journal["tree"] = snapshot(store, run, policy)
    return sorted(journal["files"])
