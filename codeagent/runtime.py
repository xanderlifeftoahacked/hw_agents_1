import json
import os
import secrets
import time

from . import budget, journal, planning, prompts
from .guard import MODES, Denied, authorize
from .policy import Policy, PolicyError
from .store import Store
from .tools import CONTROL, EXECUTING, PLANNING, SIDE_EFFECTS, TOOLS, ToolError

ALL_TOOLS = TOOLS | planning.CONTROL_TOOLS


class UserError(Exception):
    pass


def env_fault(point: str) -> None:
    if os.environ.get("AGENT_FAULT") == point:
        print(f"[fault] simulated crash at {point}", flush=True)
        os._exit(70)


def parse_arguments(raw: str) -> tuple[dict, str | None]:
    try:
        args = json.loads(raw or "{}")
    except json.JSONDecodeError as error:
        return {}, f"arguments are not valid JSON: {error}"
    if not isinstance(args, dict):
        return {}, "arguments must be a JSON object"
    return args, None


def short(text: str, lines: int = 12) -> str:
    rows = text.splitlines()
    return "\n".join(rows[:lines] + ([f"... ({len(rows) - lines} more lines)"] if len(rows) > lines else []))


class Runtime:
    def __init__(self, policy: Policy, store: Store, model=None, out=print, fault=env_fault):
        self.policy = policy
        self.store = store
        self.model = model
        self.out = out
        self.fault = fault

    def start(self, task: str, mode: str = "safe", plan: bool = False) -> dict:
        if mode not in MODES:
            raise UserError(f"unknown mode: {mode}")
        run_id = time.strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(3)
        run = {
            "run_id": run_id,
            "task": task,
            "mode": mode,
            "plan_mode": plan,
            "phase": PLANNING if plan else EXECUTING,
            "status": "running",
            "waiting": None,
            "messages": [
                {"role": "system", "content": prompts.system(self.policy, plan)},
                {"role": "user", "content": task},
            ],
            "queue": [],
            "calls": {},
            "seq": 0,
            "budget": budget.new(self.policy.budget),
            "plan": None,
            "tasks": {},
            "current_task": None,
            "final_requested": False,
            "journal": journal.new(),
            "result": None,
            "error": None,
        }
        self.save(run)
        self.out(f"run_id: {run_id} (mode {mode}{', plan mode' if plan else ''})")
        return run

    def load(self, run_id: str) -> dict:
        return self.store.load(run_id)

    def save(self, run: dict) -> None:
        self.store.save(run)

    def advance(self, run: dict) -> dict:
        self.detect_crash(run)
        while run["status"] == "running":
            try:
                self.step(run)
            except budget.Exhausted as error:
                self.fail(run, f"budget exhausted: {error}")
        return run

    def step(self, run: dict) -> None:
        if run["queue"]:
            self.process(run, run["queue"][0])
            return
        if run["phase"] == EXECUTING and run["tasks"]:
            self.schedule(run)
            if run["status"] != "running":
                return
        self.ask_model(run)

    def ask_model(self, run: dict) -> None:
        budget.spend(run, "model_requests")
        self.save(run)
        reply = self.model.complete(run["messages"], self.specs(run))
        message = {"role": "assistant", "content": reply.content}
        if reply.tool_calls:
            message["tool_calls"] = [
                {"id": c["id"], "type": "function", "function": {"name": c["name"], "arguments": c["arguments"]}}
                for c in reply.tool_calls
            ]
        run["messages"].append(message)
        if reply.content:
            self.out(f"model: {reply.content}")
        if reply.tool_calls:
            self.accept(run, reply.tool_calls)
        else:
            self.on_answer(run, reply.content)
        self.save(run)

    def specs(self, run: dict) -> list[dict]:
        return [
            tool.spec()
            for tool in ALL_TOOLS.values()
            if run["phase"] in tool.phases and (tool.name != "complete_task" or run["tasks"])
        ]

    def accept(self, run: dict, calls: list[dict]) -> None:
        side_effects = sum(1 for c in calls if c["name"] in ALL_TOOLS and ALL_TOOLS[c["name"]].kind in SIDE_EFFECTS)
        missing = budget.first_missing(run, {"tool_calls": len(calls), "side_effects": side_effects})
        if missing:
            for c in calls:
                self.reply(run, c["id"], f"error: not executed, the {missing} budget cannot cover this batch")
            raise budget.Exhausted(f"{missing}: batch of {len(calls)} tool calls does not fit into the remaining budget")
        budget.spend(run, "tool_calls", len(calls))
        seen = set(run["calls"])
        for c in calls:
            run["queue"].append({"id": c["id"], "name": c["name"], "raw": c["arguments"], "duplicate": c["id"] in seen})
            seen.add(c["id"])

    def on_answer(self, run: dict, content: str) -> None:
        if run["phase"] == PLANNING:
            run["messages"].append({"role": "user", "content": prompts.NEED_PLAN})
            return
        if run["tasks"] and not planning.all_done(run["tasks"]):
            task = run["tasks"][run["current_task"]]
            run["messages"].append({"role": "user", "content": prompts.unfinished(task)})
            return
        run["status"] = "finished"
        run["result"] = content

    def schedule(self, run: dict) -> None:
        tasks = run["tasks"]
        if run["current_task"]:
            return
        task = planning.next_task(tasks)
        if task:
            task["status"] = "running"
            run["current_task"] = task["id"]
            run["messages"].append({"role": "user", "content": prompts.task(task)})
            self.out(planning.checklist(tasks))
            self.save(run)
        elif not planning.all_done(tasks):
            self.fail(run, "no task can run: dependencies are not done")
        elif not run["final_requested"]:
            run["final_requested"] = True
            self.out(planning.checklist(tasks))
            run["messages"].append({"role": "user", "content": prompts.FINAL})
            self.save(run)

    def process(self, run: dict, item: dict) -> None:
        if item["duplicate"]:
            self.replay(run, item)
            return
        call = run["calls"].get(item["id"]) or self.register(run, item)
        self.execute(run, call)

    def register(self, run: dict, item: dict) -> dict:
        args, error = parse_arguments(item["raw"])
        call = {
            "id": item["id"],
            "name": item["name"],
            "args": args,
            "error": error,
            "phase": run["phase"],
            "task_id": run["current_task"] if run["phase"] == EXECUTING else None,
            "status": "proposed",
            "decision": None,
            "reason": None,
            "attempts": [],
            "result": None,
            "seq": None,
        }
        run["calls"][item["id"]] = call
        task = f" [{call['task_id']}]" if call["task_id"] else ""
        self.out(f"-> {call['name']}{task} {json.dumps(args, ensure_ascii=False)[:300]}")
        return call

    def replay(self, run: dict, item: dict) -> None:
        call = run["calls"][item["id"]]
        args, _ = parse_arguments(item["raw"])
        if call["name"] != item["name"] or call["args"] != args:
            text = f"error: protocol error: tool_call_id {item['id']} was already used with another tool or arguments"
        else:
            text = call["result"]["text"]
        self.reply(run, item["id"], text)
        run["queue"].pop(0)
        self.save(run)

    def execute(self, run: dict, call: dict) -> None:
        try:
            tool, prepared, confirm = authorize(self.policy, ALL_TOOLS, call, run["mode"])
        except Denied as error:
            self.finish(run, call, "denied", f"error: {error}")
            return
        if tool.kind == CONTROL:
            self.control(run, call)
        elif call["decision"] == "denied":
            self.finish(run, call, "rejected", f"error: rejected by the user: {call['reason'] or 'no reason given'}")
        elif confirm and call["decision"] is None:
            call["status"] = "awaiting_confirmation"
            self.pause(run, {"kind": "confirm", "call_id": call["id"]})
        elif tool.kind in SIDE_EFFECTS:
            self.run_side_effect(run, call, tool, prepared)
        else:
            self.run_read(run, call, tool, prepared)

    def invoke(self, tool, prepared, call: dict, limit: int) -> tuple[bool, str, dict]:
        try:
            result = tool.run(self.policy, prepared, call["args"], limit)
            return True, result.text, result.data
        except (ToolError, PolicyError, OSError) as error:
            return False, f"error: {error}", {}

    def run_read(self, run: dict, call: dict, tool, prepared) -> None:
        limit = budget.output_limit(run, self.policy.max_output)
        ok, text, data = self.invoke(tool, prepared, call, limit)
        self.complete(run, call, ok, text, data, limit)

    def run_side_effect(self, run: dict, call: dict, tool, prepared) -> None:
        before = journal.snapshot(self.store, run, self.policy)
        known = run["journal"]["tree"]
        if known is not None and before != known:
            self.pause(run, self.recovery(run, "conflict", call, known, before))
            return
        limit = budget.output_limit(run, self.policy.max_output)
        budget.spend(run, "side_effects")
        run["journal"]["tree"] = before
        attempt = {"n": len(call["attempts"]) + 1, "status": "started"}
        call["attempts"].append(attempt)
        call["status"] = "started"
        self.save(run)

        ok, text, data = self.invoke(tool, prepared, call, limit)
        self.fault("after_execute")

        after = journal.snapshot(self.store, run, self.policy)
        journal.record(self.store, run, before, after, call["id"], attempt["n"], call["task_id"])
        attempt["status"] = "completed"
        self.complete(run, call, ok, text, data, limit)

    def complete(self, run: dict, call: dict, ok: bool, text: str, data: dict, limit: int) -> None:
        text, size, truncated = budget.clip(text, limit)
        budget.spend(run, "result_bytes", size)
        run["seq"] += 1
        call["seq"] = run["seq"]
        self.finish(run, call, "completed" if ok else "failed", text, data, truncated)

    def finish(self, run: dict, call: dict, status: str, text: str, data: dict | None = None, truncated: bool = False) -> None:
        call["status"] = status
        call["result"] = {"ok": status == "completed", "text": text, "data": data or {}, "truncated": truncated}
        self.reply(run, call["id"], text)
        run["queue"].pop(0)
        self.save(run)

    def reply(self, run: dict, call_id: str, text: str) -> None:
        run["messages"].append({"role": "tool", "tool_call_id": call_id, "content": text})
        self.out("<- " + short(text).replace("\n", "\n   "))

    def control(self, run: dict, call: dict) -> None:
        if call["name"] == "submit_plan":
            try:
                planning.check_plan(self.policy, call["args"])
            except ValueError as error:
                self.finish(run, call, "failed", f"error: invalid plan: {error}")
                return
            run["plan"] = call["args"] | {"status": "proposed", "call_id": call["id"]}
            call["status"] = "awaiting_approval"
            self.pause(run, {"kind": "plan", "call_id": call["id"]})
            return
        task = run["tasks"].get(call["args"]["task_id"])
        if task is None or task["id"] != run["current_task"]:
            self.finish(run, call, "failed", f"error: {call['args']['task_id']} is not the current task ({run['current_task']})")
            return
        try:
            evidence = planning.verify(run, task)
        except ValueError as error:
            self.finish(run, call, "failed", f"error: task {task['id']} is not complete: {error}")
            return
        task["status"] = "done"
        task["evidence"] = evidence
        run["current_task"] = None
        self.finish(run, call, "completed", f"task {task['id']} is done: {evidence['detail']}")

    def recovery(self, run: dict, reason: str, call: dict, before: dict, after: dict) -> dict:
        paths = journal.changed(before, after)
        return {
            "kind": "recovery",
            "reason": reason,
            "call_id": call["id"],
            "changes": paths,
            "diff": journal.diff(self.store, run["run_id"], before, after, paths),
        }

    def detect_crash(self, run: dict) -> None:
        if run["status"] != "running":
            return
        for call in run["calls"].values():
            if call["status"] == "started":
                call["status"] = "uncertain"
                call["attempts"][-1]["status"] = "uncertain"
                current = journal.snapshot(self.store, run, self.policy)
                self.pause(run, self.recovery(run, "uncertain", call, run["journal"]["tree"], current))
                return

    def pause(self, run: dict, waiting: dict) -> None:
        run["status"] = "waiting"
        run["waiting"] = waiting
        self.save(run)

    def fail(self, run: dict, error: str) -> None:
        run["status"] = "failed"
        run["error"] = error
        run["waiting"] = None
        self.save(run)

    def waiting(self, run: dict, *kinds: str) -> dict:
        waiting = run["waiting"]
        if run["status"] != "waiting" or waiting["kind"] not in kinds:
            raise UserError(f"run {run['run_id']} is not waiting for {' or '.join(kinds)}")
        return waiting

    def resume_waiting(self, run: dict) -> None:
        run["status"] = "running"
        run["waiting"] = None
        self.save(run)

    def approve(self, run: dict) -> None:
        waiting = self.waiting(run, "confirm", "plan")
        call = run["calls"][waiting["call_id"]]
        if waiting["kind"] == "confirm":
            call["decision"] = "approved"
        else:
            run["plan"]["status"] = "approved"
            run["phase"] = EXECUTING
            run["tasks"] = planning.make_tasks(run["plan"])
            self.finish(run, call, "completed", "Plan approved by the user. Tasks will be given one by one.")
        self.resume_waiting(run)

    def reject(self, run: dict, reason: str = "") -> None:
        waiting = self.waiting(run, "confirm", "plan")
        call = run["calls"][waiting["call_id"]]
        if waiting["kind"] == "confirm":
            call["decision"] = "denied"
            call["reason"] = reason
        else:
            run["plan"]["status"] = "rejected"
            text = f"Plan rejected by the user: {reason or 'no reason given'}. Revise the plan and call submit_plan again."
            self.finish(run, call, "rejected", text)
        self.resume_waiting(run)

    def resolve(self, run: dict, action: str, changes: str | None = None) -> None:
        waiting = self.waiting(run, "recovery")
        call = run["calls"][waiting["call_id"]]
        if action not in ("retry", "fail", "abort"):
            raise UserError(f"unknown action: {action}")
        if waiting["changes"] and changes not in ("ours", "foreign"):
            raise UserError("the workspace has changed: choose --changes ours or --changes foreign")
        if action == "retry" and waiting["reason"] == "uncertain" and not ALL_TOOLS[call["name"]].retryable:
            raise UserError(f"{call['name']} is not safely retryable: choose fail or abort")
        if waiting["changes"]:
            current = journal.snapshot(self.store, run, self.policy)
            if changes == "ours":
                journal.record(self.store, run, run["journal"]["tree"], current, call["id"], len(call["attempts"]), call["task_id"])
            else:
                run["journal"]["tree"] = current
        self.resume_waiting(run)
        if action == "abort":
            self.fail(run, f"aborted by the user after {waiting['reason']} call {call['id']}")
        elif action == "fail":
            reason = "its outcome is unknown after a crash" if waiting["reason"] == "uncertain" else "the workspace was changed outside the agent"
            self.finish(run, call, "failed", f"error: the call was not completed, {reason}; treat it as failed")
        else:
            call["status"] = "proposed"
            if waiting["reason"] == "uncertain":
                call["decision"] = "approved"
            self.save(run)

    def rollback(self, run: dict) -> list[str]:
        self.detect_crash(run)
        if run["status"] == "waiting" and run["waiting"]["kind"] == "recovery":
            raise UserError("resolve the pending recovery first: agent resolve <run_id> ...")
        paths = journal.rollback(self.store, run, self.policy)
        run["status"] = "rolled_back"
        run["waiting"] = None
        self.save(run)
        return paths
