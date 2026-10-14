import hashlib
import json
import os
import re
from pathlib import Path

RUN_ID = re.compile(r"[0-9]{8}-[0-9]{6}-[0-9a-f]{6}")


class RunNotFound(Exception):
    pass


def atomic_write(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as file:
        file.write(data)
        file.flush()
        os.fsync(file.fileno())
    os.replace(tmp, path)


class Store:
    def __init__(self, root: Path):
        self.root = Path(root)

    def directory(self, run_id: str) -> Path:
        if not RUN_ID.fullmatch(run_id):
            raise RunNotFound(f"invalid run_id: {run_id}")
        return self.root / "runs" / run_id

    def save(self, run: dict) -> None:
        directory = self.directory(run["run_id"])
        directory.mkdir(parents=True, exist_ok=True)
        atomic_write(directory / "state.json", json.dumps(run, ensure_ascii=False, indent=1).encode())

    def load(self, run_id: str) -> dict:
        path = self.directory(run_id) / "state.json"
        if not path.exists():
            raise RunNotFound(f"run not found: {run_id}")
        return json.loads(path.read_text())

    def runs(self) -> list[dict]:
        root = self.root / "runs"
        if not root.exists():
            return []
        return [self.load(p.name) for p in sorted(root.iterdir()) if (p / "state.json").exists()]

    def put_blob(self, run_id: str, data: bytes) -> str:
        digest = hashlib.sha256(data).hexdigest()
        path = self.directory(run_id) / "blobs" / digest
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(path, data)
        return digest

    def blob(self, run_id: str, digest: str) -> bytes:
        return (self.directory(run_id) / "blobs" / digest).read_bytes()
