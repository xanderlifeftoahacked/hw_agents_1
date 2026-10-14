# Coding agent with tool calling

A console agent for small changes in a project. It picks tools on its own, reads and edits files, runs tests and decides when the work is done. The model only proposes tool calls. The runtime makes the decision: it checks the argument schema, the policy, the mode, the budget and the journal, saves the state, and only then executes the action.

No agent frameworks are used. The loop, policy, recovery, budgets, planning, tasks and journal are implemented in `codeagent/`. The only model dependency is the `openai` SDK (OpenAI-compatible Chat Completions).

## Requirements coverage

| Requirement | Implementation | Tests |
|---|---|---|
| Block 1. Agent loop and tools | `runtime.py` (`advance`, `step`, `ask_model`, `accept`, `process`), `tools.py` | `test_loop.py` |
| Block 2. Security, safe/auto modes, policy | `policy.py`, `guard.py`, `policy.toml`, `Runtime.approve` / `Runtime.reject` | `test_policy.py`, `test_modes.py` |
| Block 3. Crash recovery | `store.py`, `Runtime.run_side_effect`, `Runtime.detect_crash`, `Runtime.resolve` | `test_recovery.py` |
| Block 4. Plan Mode | `planning.py` (`PLAN_SCHEMA`, `check_plan`), `submit_plan` control tool | `test_plan.py` |
| 8 points. Budgets | `budget.py`, budget checks in `Runtime.accept`, `run_read`, `run_side_effect` | `test_budget.py` |
| 9 points. Tasks with dependencies | `planning.py` (`make_tasks`, `next_task`, `verify`, `checklist`), `Runtime.schedule`, `complete_task` | `test_tasks.py` |
| 10 points. Change journal and rollback | `journal.py`, `Runtime.rollback` | `test_journal.py` |
| Interactive mode, CLI | `cli.py` | `test_cli.py` |

## Layout

```
codeagent/
  runtime.py    agent loop: model requests, tool call queue, confirmations, recovery, rollback
  guard.py      checks before execution: name, JSON Schema, phase, policy, mode
  policy.py     loads policy.toml, checks paths, commands and directories
  tools.py      tools with JSON Schemas, Bash runner (timeout, output limit, bwrap)
  planning.py   Plan Mode: plan schema and validation, tasks, task result checks, checklist
  budget.py     limits and spending
  journal.py    workspace snapshots, diff, rollback
  store.py      run state storage (atomic JSON writes) and file contents
  llm.py        OpenAI-compatible client and ScriptedModel for deterministic tests
  prompts.py    system prompt and messages to the model
  cli.py        CLI commands and interactive mode
policy.toml     example policy (outside the workspace)
workspace/      control project: order.py, test_order.py
tests/          automated runtime tests
agent           launcher script
```

## Installation

Everything is installed inside the project directory: Python dependencies in `.venv/`, Ollama and models in `.ollama/`, run state in `.agent/`. Requirements: Linux x86_64 (or WSL), Python 3.11+, bash, curl, zstd, and `bwrap` (bubblewrap) for Bash isolation.

```bash
make setup             # .venv + requirements.txt, .env from .env.example
make ollama-install    # download Ollama into .ollama/ (~1.4 GB)
make ollama-serve      # in a separate terminal: local server on 127.0.0.1:11434
make ollama-pull       # download the OPENAI_MODEL model (qwen3:4b by default) into .ollama/models
```

The model is configured in `.env` (see `.env.example`):

```
OPENAI_BASE_URL=http://localhost:11434/v1/
OPENAI_API_KEY=ollama
OPENAI_MODEL=qwen3:4b
```

Variables already set in the environment take precedence over `.env`. On a CPU, `qwen3:4b` (Qwen3-4B-Thinking) reasons at length before every step, so a single request takes minutes. A non-thinking model is more practical for a demo:

```bash
OPENAI_MODEL=qwen3:4b-instruct ./scripts/ollama.sh pull
OPENAI_MODEL=qwen3:4b-instruct ./agent run --mode auto "..."
```

The server starts with a 16k context (`OLLAMA_CONTEXT_LENGTH`). With the default 4k, qwen3 reasoning pushes the system prompt out of the context.

## Tools

| Tool | Kind | Retry after crash | Description |
|---|---|---|---|
| `list_files(path)` | read | yes | recursive file list without denied directories and generated files |
| `read_file(path)` | read | yes | reads a text file |
| `write_file(path, content)` | write | yes | creates or overwrites a file with full content |
| `edit_file(path, old, new)` | write | no | replaces exactly one occurrence of `old` |
| `delete_file(path)` | delete | yes | deletes a file |
| `Bash(command, cwd)` | exec | no | runs an allowed command with `bash`, returns `exit_code`, stdout and stderr |
| `submit_plan(...)` | control | — | Plan Mode only: submit the structured plan |
| `complete_task(task_id, summary)` | control | — | ask the runtime to verify the result of the current task |

Every tool is passed to the model with a name, a description and a JSON Schema (`additionalProperties: false`). Paths are relative to `workspace/`.

Why these tools are retryable. Reads have no side effects. `write_file` writes the full content, so a retry gives the same result. A retried `delete_file` leaves the same final state: the file is gone. `edit_file` is not idempotent, because a second replacement would apply to already changed text. Bash runs an arbitrary program with unknown effects, so it must not be retried blindly.

## Running the agent

```bash
./agent run "Исправь расчёт итоговой стоимости заказа, чтобы тесты проходили. Тесты не меняй. Сам найди нужные файлы, внеси исправление и запусти тесты."
./agent run --mode auto "..."          # writes and Bash without confirmation
./agent run --plan "..."               # start in Plan Mode
./agent chat --mode safe --plan        # interactive mode
./agent runs                           # list runs
./agent status <run_id>                # run state, budget, tasks, pending decision
```

### Interactive mode

`./agent chat` opens a REPL. A line without `/` starts a new run with that task. Action confirmations, plan approval and crash decisions are asked right in the terminal. The runtime works the same way as in the plain CLI and saves the state before every question, so a run interrupted with Ctrl+C can be continued with `/resume` or `./agent resume <run_id>`.

```
/mode safe|auto   /plan on|off   /runs   /resume ID
/status [ID]   /tasks [ID]   /diff [ID]   /rollback [ID]   /help   /exit
```

## Modes and action confirmation

| Action | safe | auto |
|---|---|---|
| read | immediately | immediately |
| write, Bash | after confirmation | immediately if the policy allows it |
| delete | after confirmation | after confirmation |

When a confirmation is needed, the runtime saves the state with status `waiting` and stops. For writes the CLI shows the diff of the future file, for Bash the command and cwd.

```bash
./agent approve <run_id>                      # allow, the same run continues
./agent reject <run_id> --reason "do not touch order.py"
```

After a rejection the model receives the tool result `error: rejected by the user: <reason>` with the original `tool_call_id` and can choose another step. Confirmation does not skip the checks: before execution the runtime checks the schema, policy and mode again. If the policy has changed in the meantime, the action is denied.

## Policy

`policy.toml` lives outside `workspace/`, so the agent tools cannot change it: all paths resolve only inside the allowed workspace directories. On load the runtime refuses to start if the policy file or the state directory is inside the workspace, or if an allowed or denied directory points outside it.

```toml
workspace = "workspace"
state_dir = ".agent"

[commands]
allow = ["python3 -m unittest", "python3 -m unittest -v", "python3 -m unittest -v test_order", "ls", "ls -la", "pwd"]
deny = ["rm", "bash", "sh", "python3 -c", "curl", "wget", "sudo", "git push"]

[directories]
allow = ["workspace"]
deny = ["workspace/private"]

[files]
read_only = ["workspace/test_order.py"]

[journal]
generated = ["__pycache__", "*.pyc"]

[bash]
timeout_seconds = 30
max_output_bytes = 20000
sandbox = "bwrap"

[budget]
model_requests = 40
tool_calls = 80
side_effects = 25
result_bytes = 200000
```

Rules:

- `commands.allow` lists exact allowed commands. A command is normalized (extra spaces removed) and compared as a whole string. Anything not in the list is denied.
- `commands.deny` lists denied word sequences anywhere in the command. Deny wins over allow: if `rm order.py` is in `allow` and `rm` is in `deny`, the command is denied.
- Any shell syntax is denied: `| & ; < > ( ) $ \` \ " ' * ? [ ] { } ~ ! #` and newlines. Pipes, substitutions, redirects, `&&`, globs and `bash -c "..."` do not pass. Arguments with an absolute path or `..` are denied too.
- `directories.allow` and `directories.deny` are canonical paths inside the workspace. Deny wins over allow. File tools and the Bash `cwd` must be inside an allowed directory and outside every denied one.
- A path is denied if it is absolute, contains `..` or goes through a symlink (every path component is checked).
- `files.read_only` lists files the agent may read but not change (here: the control project tests).
- `journal.generated` lists generated files the journal does not track. On load the runtime checks that these patterns hide no `.py` file.

The checks live in `policy.py` and `guard.py`, separate from the loop in `runtime.py`. `guard.authorize()` runs before every execution, including after a confirmation and after resume.

## How the system is protected from the agent

1. **The runtime is the gatekeeper.** Every tool call is untrusted input. Check order: known name → valid JSON → JSON Schema → allowed in the current phase → policy (paths, command, cwd) → mode and confirmation → budget → journal → execution.
2. **Files.** Only relative paths inside allowed workspace directories, without `..` and symlinks. The policy, the run state and `.venv` are outside the workspace.
3. **Bash.** Only exact allowlisted commands without shell syntax. Commands run through `bash --noprofile --norc -c` with a clean environment: no API keys, `HOME=cwd`, `PYTHONDONTWRITEBYTECODE=1`. A timeout kills the whole process group, and the output is cut to the limit.
4. **Process isolation (`sandbox = "bwrap"`).** Command checks do not stop an allowed program from doing anything with the user's permissions. For example, `python3 -m unittest` executes whatever code the agent has just written to `order.py`. That is why Bash runs inside bubblewrap: the whole file system is read-only, `/home`, `/root`, `/run` and `/tmp` are replaced with empty tmpfs, only the workspace is writable, there is no network (`--unshare-all`), and the process dies together with the agent. Without bwrap, `sandbox = "none"` is available, but then the workspace must be a training copy without secrets.
5. **Human in the loop.** In safe mode every write and every Bash call is confirmed, with a diff shown first. Deletes are confirmed in both modes.
6. **Budget and journal.** The budget stops endless loops. The journal makes it possible to roll back the run and detects outside edits.

## Plan Mode and plan approval

```bash
./agent run --plan "..."
./agent status <run_id>                     # plan: goal, steps, tools, checks, risks, assumptions, done criteria
./agent approve <run_id>                    # approve the plan and start execution
./agent reject <run_id> --reason "..."      # reject: the model gets the reason and submits a new plan
```

In the `planning` phase the model gets only `list_files`, `read_file` and `submit_plan`. If it calls any other tool, the runtime answers `not available in the planning phase` and executes nothing: the phase is stored in every call and checked in `guard.py`. The plan is validated against a JSON Schema and then checked again. Step ids must be unique, dependencies may only point to earlier steps (so there are no cycles), tool names must exist, and every step check must pass the policy. The plan is stored in the run `state.json` as structured data:

```json
{"goal": "...", "steps": [{"id": "T1", "description": "...", "tools": ["read_file"], "depends_on": [],
  "check": {"kind": "file_read", "target": "order.py"}}], "risks": [], "assumptions": [], "done_criteria": "...", "status": "approved"}
```

Approving the plan does not grant any permissions in advance: in safe mode every write and every Bash call still needs confirmation.

## Tasks

After approval the plan steps become tasks with the fields `id`, `description`, `depends_on`, `tools`, `check`, `status` (`pending` → `running` → `done`) and `evidence`. The runtime gives tasks to the model one at a time, and only after all dependencies are done. Every tool call in the execution phase gets the `task_id` of the current task. Planning calls are stored with `phase: planning` and `task_id: null`.

The model saying "done" is not enough. `complete_task` makes the runtime look for proof among the recorded facts of the current task:

| check | Proven by |
|---|---|
| `file_read <path>` | a successful `read_file` of this file in the task |
| `file_changed <path>` | a journal entry for this file in the task |
| `command_ok <command>` | a successful `Bash` with this command and `exit_code 0` in the task, run after the last file change of the run |

The proof is saved in `evidence` (call id and description). Without proof the model gets an error and the task stays in progress. The checklist is printed to stdout when a task starts and when all tasks are done:

```
[x] T1 Read order.py
[~] T2 Fix order.py (after T1)
[ ] T3 Run the tests (after T2)
```

```bash
./agent tasks <run_id>     # checklist and evidence
```

After resume, done tasks are not run again: their status is part of the run state.

## Budgets

Limits are set in the `[budget]` section of the policy and copied into the run state when the run is created:

- `model_requests` — model requests, spent and saved before the request is sent.
- `tool_calls` — tool calls, spent for the whole batch when the model reply arrives.
- `side_effects` — writes, deletes and Bash, spent in the same atomic state write that records the `started` status.
- `result_bytes` — total size of the results of executed tools.

The budget is shared by all phases of one `run_id`. Resume, plan approval and retry after a crash do not reset the counters. The state is saved with one atomic replace of `state.json`, so the limit check, the spending and the action status are recorded together. If the process crashes after spending, the budget is not returned.

Edge cases:

- If the model returns a batch of calls and the remaining `tool_calls` or `side_effects` cannot cover the whole batch, none of the calls runs, and the run fails with a budget error.
- The output size is unknown in advance, so the tool gets `min(max_output_bytes, remaining result_bytes)`. The runtime cuts the output to that size and appends `[truncated: ... the result is incomplete]`, and the result gets the flag `truncated: true`. If nothing remains, the action does not start.

## Recovery by run_id

The state of each run is stored in `.agent/runs/<run_id>/state.json`: message history, tool call queue, calls with attempts, user decisions, plan, tasks, budget and journal. File contents for snapshots are stored in `.agent/runs/<run_id>/blobs/`.

Call statuses: `proposed`, `awaiting_confirmation`, `awaiting_approval`, `started`, `completed`, `failed`, `denied` (by the runtime), `rejected` (by the user), `uncertain`. Attempt statuses: `started`, `completed`, `uncertain`.

Before an action with side effects the runtime saves an attempt with status `started`, and after execution it saves the result. If the process crashes between these writes, the next `resume` marks the call `uncertain` and does not silently run it again. The runtime compares the workspace with the last snapshot and shows the detected changes:

```bash
./agent resume <run_id>
./agent resolve <run_id> retry  --changes ours      # retry (write_file, delete_file only); a new attempt of the same tool call
./agent resolve <run_id> fail   --changes foreign   # treat the step as failed, the model gets an error and continues
./agent resolve <run_id> abort  --changes ours      # finish the run with an error
```

`--changes` is required when the workspace has changed. `ours` attributes the changes to the run (they go into the journal and rollback), `foreign` accepts them as outside changes.

A repeated `tool_call_id` with the same name and arguments does not execute the tool again: the model gets the saved result. If the name or arguments differ, the model gets a `protocol error`.

Crash simulation: `AGENT_FAULT=after_execute` kills the process right after a tool runs and before its result is saved.

```bash
AGENT_FAULT=after_execute ./agent run --mode auto "..."   # the process dies after the first write
./agent resume <run_id>                                   # the call is uncertain, a diff is shown
./agent resolve <run_id> retry --changes ours
```

## Change journal and rollback

Before every action that can change files (`write_file`, `edit_file`, `delete_file`, `Bash`) the runtime takes a workspace snapshot: paths, hashes and file contents. The snapshot is compared with the last saved one. If files changed outside the agent, the action does not run and the run waits for `resolve` (`reason: conflict`). After the action the runtime saves a new snapshot, the diff and the hashes. For every touched file the journal keeps the hash before the first change (`before`) and after the last one (`after`); `null` means the file does not exist. This is how creation and deletion are recorded.

```bash
./agent diff <run_id>        # net diff, file hashes and the per-step journal
./agent rollback <run_id>    # roll back the run changes
```

Rollback touches only files from this run's journal. Before writing, the runtime checks the policy for every path again and compares the current hash with `after`. If at least one file was changed from outside, rollback overwrites nothing and lists the conflicts. Rollback is refused until an uncertain call is resolved with `resolve`. The journal is stored with the run state, so `diff` and `rollback` work after a restart.

Generated files (`__pycache__`, `*.pyc`) are explicitly excluded from snapshots. Bash also runs with `PYTHONDONTWRITEBYTECODE=1`.

## Tests

```bash
make test        # or .venv/bin/python -m pytest -v
```

The tests are deterministic: `ScriptedModel` replays prepared replies instead of a real model, and every test gets its own copy of the workspace in a temporary directory.

| File | What it shows |
|---|---|
| `test_loop.py` | control task: the agent reads files, fixes `order.py`, verifies the fix with Bash, `test_order.py` stays unchanged; several tool calls in one reply; `role: tool` with the original `tool_call_id`; tool errors do not crash the process; Bash timeout |
| `test_policy.py` | allowed command; denied command; deny priority; shell syntax and `bash -c`; allowed command in a denied directory; denied subdirectory for a file tool; `..`, absolute path, symlink; the policy cannot be changed; read-only tests; bwrap isolation |
| `test_modes.py` | safe mode stops and continues the same run after a restart; rejection with a reason; auto mode; delete needs confirmation in auto; policy re-check after confirmation |
| `test_recovery.py` | controlled crash between execution and saving; `uncertain`; retry as a new attempt; Bash is not retryable; fail and abort; completed actions are not repeated; repeated `tool_call_id`; protocol error |
| `test_plan.py` | no side effects before plan approval; plan structure; invalid plans; plan rejection; confirmations in safe mode after approval |
| `test_tasks.py` | three tasks with dependencies; task result verification; `task_id` on calls; tasks do not start before their dependencies and are not repeated after resume; checklist |
| `test_budget.py` | exhaustion of every limit; a batch that does not fit; output truncation; counters after resume and plan approval; crash after spending |
| `test_journal.py` | diff of the `order.py` change; rollback after a restart; conflict on an outside edit; created and deleted files; conflict between actions; an uncertain call blocks rollback |
| `test_cli.py` | interactive session with confirmations, diff and rollback; rejection with a reason; CLI views after a restart |
