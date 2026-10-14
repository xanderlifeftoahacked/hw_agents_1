from .policy import Policy

BASE = """You are a coding agent. You work only inside the project workspace through the provided tools.
All paths are relative to the workspace root, for example "order.py". Absolute paths and ".." are rejected.
The runtime checks every tool call. If a call is denied or fails, read the error and choose another step.
Bash runs only these exact commands, without pipes, redirects or any other shell syntax. Use cwd ".":
{commands}
Never modify test files.
Before finishing, run the tests with Bash and make sure they pass.
When the work is done, answer with a short summary and no tool calls."""

PLAN = """

PLAN MODE: the plan is not approved yet. Use only list_files and read_file to study the project.
Then call submit_plan once with:
- goal
- steps in execution order; each step has id (T1, T2, ...), description, tools (tool names such as "read_file", "edit_file", "Bash"), depends_on (ids of earlier steps) and check
- risks, assumptions, done_criteria
check is how the runtime verifies the result of a step, one of:
  {"kind": "file_read", "target": "<path>"}: the file was read with read_file during the step
  {"kind": "file_changed", "target": "<path>"}: the file was changed during the step
  {"kind": "command_ok", "target": "<allowed command>"}: the command exited with code 0 during the step after the last file change
Do not change files and do not run Bash until the plan is approved."""

NEED_PLAN = "Plan mode is active. Study the project with read-only tools and call submit_plan with the structured plan."

FINAL = "All tasks are done and verified. Reply with a short final summary without tool calls."


def system(policy: Policy, plan: bool) -> str:
    commands = "\n".join(f"- {command}" for command in policy.allow_commands)
    return BASE.format(commands=commands) + (PLAN if plan else "")


HOW_TO_CHECK = {
    "file_read": 'call read_file with path "{target}"',
    "file_changed": 'change "{target}" with write_file or edit_file',
    "command_ok": 'call Bash with command "{target}" and cwd "." and make sure exit_code is 0',
}


def next_steps(task: dict) -> str:
    check = task["check"]
    action = HOW_TO_CHECK[check["kind"]].format(target=check["target"])
    return (
        f"Only tool calls made during task {task['id']} count, earlier calls do not. "
        f"Reply with tool calls, not text: {action}, then call complete_task with task_id \"{task['id']}\"."
    )


def task(task: dict) -> str:
    return (
        f"The plan is approved. Current task {task['id']}: {task['description']}\n"
        f"Suggested tools: {', '.join(task['tools']) or '-'}\n"
        f"Work only on this task. {next_steps(task)}"
    )


def unfinished(task: dict) -> str:
    return f"Task {task['id']} is not completed yet. {next_steps(task)}"
