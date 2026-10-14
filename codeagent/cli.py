import argparse
import difflib
import json
import os
import sys

from . import budget, journal, planning
from .guard import MODES
from .journal import RollbackConflict
from .llm import OpenAIModel
from .policy import PolicyError, load_policy
from .runtime import Runtime, UserError
from .store import RunNotFound, Store

ERRORS = (UserError, PolicyError, RunNotFound, RollbackConflict)

HELP = """Type a task for the agent or a command:
  /mode safe|auto     mode for new runs
  /plan on|off        Plan Mode for new runs
  /runs               list runs
  /resume ID          continue a run
  /status [ID]        run state
  /tasks [ID]         run tasks
  /diff [ID]          run changes
  /rollback [ID]      roll back run changes
  /help, /exit"""


def short_hash(digest: str | None) -> str:
    return digest[:10] if digest else "no file"


def preview(runtime: Runtime, call: dict) -> str:
    args = call["args"]
    if call["name"] == "write_file":
        try:
            path = runtime.policy.resolve(args["path"])
            old = path.read_text() if path.is_file() else ""
        except (PolicyError, OSError):
            old = ""
        lines = difflib.unified_diff(
            old.splitlines(keepends=True), args["content"].splitlines(keepends=True), f"a/{args['path']}", f"b/{args['path']}"
        )
        return "".join(line if line.endswith("\n") else line + "\n" for line in lines) or "(content does not change)"
    if call["name"] == "edit_file":
        return f"{args['path']}\n--- before\n{args['old']}\n+++ after\n{args['new']}"
    if call["name"] == "Bash":
        return f"$ {args['command']}    (cwd: {args['cwd']})"
    return json.dumps(args, ensure_ascii=False, indent=2)


def describe_waiting(runtime: Runtime, run: dict) -> str:
    waiting = run["waiting"]
    call = run["calls"][waiting["call_id"]]
    if waiting["kind"] == "confirm":
        return f"\nConfirmation required: {call['name']} (mode {run['mode']})\n{preview(runtime, call)}"
    if waiting["kind"] == "plan":
        return "\nPlan awaits approval:\n" + planning.render_plan(run["plan"])
    what = "the call outcome is unknown after a crash" if waiting["reason"] == "uncertain" else "the workspace was changed outside the agent"
    lines = [f"\nDecision required for {call['name']} {call['id']}: {what}"]
    if waiting["changes"]:
        lines.append("Detected changes: " + ", ".join(waiting["changes"]))
        lines.append(waiting["diff"])
    else:
        lines.append("No changes detected in the workspace.")
    return "\n".join(lines)


def hint(run: dict) -> str:
    run_id = run["run_id"]
    if run["waiting"]["kind"] in ("confirm", "plan"):
        return f"Approve: ./agent approve {run_id}\nReject:  ./agent reject {run_id} --reason \"...\""
    changes = " --changes ours|foreign" if run["waiting"]["changes"] else ""
    return f"Resolve: ./agent resolve {run_id} retry|fail|abort{changes}"


def report(runtime: Runtime, run: dict, interactive: bool = False) -> None:
    status = run["status"]
    if status == "waiting":
        print(describe_waiting(runtime, run))
        if not interactive:
            print(hint(run))
    elif status == "finished":
        print(f"\nDone: {run['result']}")
    elif status == "failed":
        print(f"\nRun failed: {run['error']}")
    print(f"run_id: {run['run_id']}, status: {status}, budget: {budget.render(run)}")


def advance(runtime: Runtime, run: dict) -> bool:
    try:
        runtime.advance(run)
        return True
    except ERRORS:
        raise
    except Exception as error:
        print(f"\nModel error: {type(error).__name__}: {error}")
        print(f"State saved, continue with: ./agent resume {run['run_id']}")
        return False


def show_status(runtime: Runtime, run: dict) -> None:
    print(f"run_id: {run['run_id']}")
    print(f"task: {run['task']}")
    print(f"mode: {run['mode']}, phase: {run['phase']}, status: {run['status']}")
    print(f"budget: {budget.render(run)}")
    if run["plan"]:
        print(f"plan: {run['plan']['status']}")
    if run["tasks"]:
        print(planning.checklist(run["tasks"]))
    if run["status"] == "waiting":
        print(describe_waiting(runtime, run))
        print(hint(run))
    if run["result"]:
        print(f"result: {run['result']}")
    if run["error"]:
        print(f"error: {run['error']}")


def show_tasks(runtime: Runtime, run: dict) -> None:
    if not run["tasks"]:
        print("the run has no tasks (they appear after the plan is approved)")
        return
    print(planning.checklist(run["tasks"]))
    for task in run["tasks"].values():
        if task["evidence"]:
            print(f"  {task['id']}: {task['evidence']['detail']}")


def show_diff(runtime: Runtime, run: dict) -> None:
    files = run["journal"]["files"]
    if not files:
        print("the run did not change any files")
        return
    print(journal.net_diff(runtime.store, run))
    print("Files:")
    for path, entry in files.items():
        current = journal.file_hash(runtime.policy.workspace / path)
        state = "matches the journal" if current == entry["after"] else "changed outside the agent"
        print(f"  {path}: {short_hash(entry['before'])} -> {short_hash(entry['after'])} (now {state})")
    print("Journal:")
    for change in run["journal"]["changes"]:
        task = f", task {change['task_id']}" if change["task_id"] else ""
        print(f"  #{change['seq']} {change['call_id']} attempt {change['attempt']}{task}: {', '.join(change['paths'])}")
    if run["journal"]["rolled_back"]:
        print("the run changes are rolled back")


def do_rollback(runtime: Runtime, run: dict) -> None:
    try:
        paths = runtime.rollback(run)
    except RollbackConflict as error:
        print("Rollback cancelled, no file was changed:")
        for problem in error.problems:
            print(f"  {problem}")
        return
    print("Restored: " + (", ".join(paths) or "nothing to restore"))


def show_runs(runtime: Runtime) -> None:
    for run in runtime.store.runs():
        print(f"{run['run_id']}  {run['status']:<12} {run['mode']:<5} {run['task'][:60]}")


VIEWS = {"status": show_status, "tasks": show_tasks, "diff": show_diff, "rollback": do_rollback}


def ask(prompt: str, options: tuple) -> str:
    while True:
        try:
            answer = input(prompt).strip().lower()
        except EOFError:
            raise KeyboardInterrupt
        if answer in options:
            return answer


def drive(runtime: Runtime, run: dict) -> None:
    while advance(runtime, run):
        if run["status"] != "waiting":
            report(runtime, run, interactive=True)
            return
        print(describe_waiting(runtime, run))
        if run["waiting"]["kind"] in ("confirm", "plan"):
            question = "Approve the plan? [y/n]: " if run["waiting"]["kind"] == "plan" else "Allow? [y/n]: "
            if ask(question, ("y", "n")) == "y":
                runtime.approve(run)
            else:
                runtime.reject(run, input("Reason: ").strip())
            continue
        changes = None
        if run["waiting"]["changes"]:
            changes = ask("Attribute these changes to the run? [ours/foreign]: ", ("ours", "foreign"))
        action = ask("What to do with the call? [retry/fail/abort]: ", ("retry", "fail", "abort"))
        try:
            runtime.resolve(run, action, changes)
        except UserError as error:
            print(error)


def chat(runtime: Runtime, mode: str, plan: bool) -> None:
    print(HELP)
    last = None
    while True:
        try:
            line = input(f"\n[{mode}{', plan' if plan else ''}]> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not line:
            continue
        command, _, arg = line[1:].partition(" ")
        arg = arg.strip()
        try:
            if not line.startswith("/"):
                run = runtime.start(line, mode, plan)
                last = run["run_id"]
                drive(runtime, run)
            elif command == "exit":
                return
            elif command == "help":
                print(HELP)
            elif command == "mode" and arg in MODES:
                mode = arg
            elif command == "plan" and arg in ("on", "off"):
                plan = arg == "on"
            elif command == "runs":
                show_runs(runtime)
            elif command in ("resume", *VIEWS):
                run = runtime.load(arg or last or "")
                last = run["run_id"]
                if command == "resume":
                    drive(runtime, run)
                else:
                    VIEWS[command](runtime, run)
            else:
                print("unknown command, see /help")
        except KeyboardInterrupt:
            print("\ninterrupted, state saved")
        except ERRORS as error:
            print(f"error: {error}")


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="agent", description="Console coding agent with tool calling")
    root.add_argument("--policy", default=os.environ.get("AGENT_POLICY", "policy.toml"))
    sub = root.add_subparsers(dest="command", required=True)

    for name in ("run", "chat"):
        command = sub.add_parser(name)
        if name == "run":
            command.add_argument("task")
        command.add_argument("--mode", choices=MODES, default="safe")
        command.add_argument("--plan", action="store_true", help="start in Plan Mode")

    sub.add_parser("runs")
    for name in ("status", "resume", "approve", "tasks", "diff", "rollback"):
        sub.add_parser(name).add_argument("run_id")
    reject = sub.add_parser("reject")
    reject.add_argument("run_id")
    reject.add_argument("--reason", default="")
    resolve = sub.add_parser("resolve")
    resolve.add_argument("run_id")
    resolve.add_argument("action", choices=("retry", "fail", "abort"))
    resolve.add_argument("--changes", choices=("ours", "foreign"))
    return root


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    try:
        policy = load_policy(args.policy)
        runtime = Runtime(policy, Store(policy.state_dir), OpenAIModel())
        if args.command == "chat":
            chat(runtime, args.mode, args.plan)
            return 0
        if args.command == "runs":
            show_runs(runtime)
            return 0
        if args.command == "run":
            run = runtime.start(args.task, args.mode, args.plan)
        else:
            run = runtime.load(args.run_id)
        if args.command in VIEWS:
            VIEWS[args.command](runtime, run)
            return 0
        if args.command == "approve":
            runtime.approve(run)
        elif args.command == "reject":
            runtime.reject(run, args.reason)
        elif args.command == "resolve":
            runtime.resolve(run, args.action, args.changes)
        if advance(runtime, run):
            report(runtime, run)
        return 0 if run["status"] in ("finished", "waiting") else 1
    except ERRORS as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
