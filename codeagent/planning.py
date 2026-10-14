import os

from .policy import Policy, PolicyError
from .tools import CONTROL, EXECUTING, PLANNING, TOOLS, Tool

CHECK_KINDS = ["file_read", "file_changed", "command_ok"]

STRINGS = {"type": "array", "items": {"type": "string"}}

TOOL_NAMES = {
    "type": "array",
    "items": {"enum": [*TOOLS, "complete_task"]},
    "description": "Names of the tools for this step, for example [\"read_file\"] or [\"Bash\"]; commands go to check.target",
}

PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "goal": {"type": "string", "minLength": 1},
        "steps": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "minLength": 1},
                    "description": {"type": "string", "minLength": 1},
                    "tools": TOOL_NAMES,
                    "depends_on": STRINGS,
                    "check": {
                        "type": "object",
                        "properties": {
                            "kind": {"enum": CHECK_KINDS},
                            "target": {"type": "string", "minLength": 1},
                        },
                        "required": ["kind", "target"],
                        "additionalProperties": False,
                    },
                },
                "required": ["id", "description", "tools", "depends_on", "check"],
                "additionalProperties": False,
            },
        },
        "risks": STRINGS,
        "assumptions": STRINGS,
        "done_criteria": {"type": "string", "minLength": 1},
    },
    "required": ["goal", "steps", "risks", "assumptions", "done_criteria"],
    "additionalProperties": False,
}

CONTROL_TOOLS = {
    "submit_plan": Tool(
        "submit_plan",
        "Submit the structured work plan for user approval. Available only in plan mode.",
        PLAN_SCHEMA, CONTROL, (PLANNING,), False,
    ),
    "complete_task": Tool(
        "complete_task",
        "Ask the runtime to verify the check of the current task and mark it done.",
        {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Id of the current task"},
                "summary": {"type": "string", "description": "What was done"},
            },
            "required": ["task_id", "summary"],
            "additionalProperties": False,
        },
        CONTROL, (EXECUTING,), False,
    ),
}


def same_path(a: str, b: str) -> bool:
    return os.path.normpath(a) == os.path.normpath(b)


def check_plan(policy: Policy, plan: dict) -> None:
    seen = set()
    for step in plan["steps"]:
        if step["id"] in seen:
            raise ValueError(f"duplicate step id: {step['id']}")
        for dependency in step["depends_on"]:
            if dependency not in seen:
                raise ValueError(f"step {step['id']} depends on {dependency}, which is not an earlier step")
        kind, target = step["check"]["kind"], step["check"]["target"]
        try:
            if kind == "command_ok":
                policy.check_command(target, ".")
            else:
                policy.resolve(target)
        except PolicyError as error:
            raise ValueError(f"step {step['id']} has a check the policy does not allow: {error}")
        seen.add(step["id"])


def make_tasks(plan: dict) -> dict:
    return {
        step["id"]: {
            "id": step["id"],
            "description": step["description"],
            "depends_on": step["depends_on"],
            "tools": step["tools"],
            "check": step["check"],
            "status": "pending",
            "evidence": None,
        }
        for step in plan["steps"]
    }


def next_task(tasks: dict) -> dict | None:
    for task in tasks.values():
        if task["status"] == "pending" and all(tasks[d]["status"] == "done" for d in task["depends_on"]):
            return task
    return None


def all_done(tasks: dict) -> bool:
    return all(task["status"] == "done" for task in tasks.values())


def verify(run: dict, task: dict) -> dict:
    kind, target = task["check"]["kind"], task["check"]["target"]
    calls = [c for c in run["calls"].values() if c["task_id"] == task["id"] and c["status"] == "completed"]
    changes = run["journal"]["changes"]
    if kind == "file_read":
        for call in calls:
            if call["name"] == "read_file" and same_path(call["args"]["path"], target):
                return {"call_id": call["id"], "detail": f"{target} was read by {call['id']}"}
    if kind == "file_changed":
        for change in changes:
            if change["task_id"] == task["id"] and os.path.normpath(target) in change["paths"]:
                return {"call_id": change["call_id"], "detail": f"{target} was changed by {change['call_id']}"}
    if kind == "command_ok":
        last_change = max((change["seq"] for change in changes), default=0)
        for call in reversed(calls):
            if (
                call["name"] == "Bash"
                and " ".join(call["args"]["command"].split()) == " ".join(target.split())
                and call["result"]["data"].get("exit_code") == 0
                and call["seq"] > last_change
            ):
                return {"call_id": call["id"], "detail": f"'{target}' exited with code 0 in {call['id']}"}
    raise ValueError(f"no recorded tool result in task {task['id']} satisfies the check {kind} {target}")


MARKS = {"pending": " ", "running": "~", "done": "x"}


def checklist(tasks: dict) -> str:
    lines = []
    for task in tasks.values():
        line = f"[{MARKS[task['status']]}] {task['id']} {task['description']}"
        if task["depends_on"]:
            line += f" (after {', '.join(task['depends_on'])})"
        lines.append(line)
    return "\n".join(lines)


def render_plan(plan: dict) -> str:
    lines = [f"Goal: {plan['goal']}", "Steps:"]
    for step in plan["steps"]:
        after = f" (after {', '.join(step['depends_on'])})" if step["depends_on"] else ""
        lines.append(f"  {step['id']}. {step['description']}{after}")
        lines.append(f"      tools: {', '.join(step['tools']) or '-'}; check: {step['check']['kind']} {step['check']['target']}")
    lines.append("Risks: " + ("; ".join(plan["risks"]) or "-"))
    lines.append("Assumptions: " + ("; ".join(plan["assumptions"]) or "-"))
    lines.append(f"Done criteria: {plan['done_criteria']}")
    return "\n".join(lines)
