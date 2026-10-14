from jsonschema import Draft202012Validator

from .policy import Policy, PolicyError
from .tools import CONTROL, DELETE, READ, Tool

MODES = ("safe", "auto")


class Denied(Exception):
    pass


def needs_confirmation(kind: str, mode: str) -> bool:
    if kind in (READ, CONTROL):
        return False
    if kind == DELETE:
        return True
    return mode == "safe"


def authorize(policy: Policy, tools: dict[str, Tool], call: dict, mode: str):
    tool = tools.get(call["name"])
    if tool is None:
        raise Denied(f"unknown tool: {call['name']}")
    if call["error"]:
        raise Denied(call["error"])
    errors = sorted(e.message for e in Draft202012Validator(tool.schema).iter_errors(call["args"]))
    if errors:
        raise Denied("invalid arguments: " + "; ".join(errors))
    if call["phase"] not in tool.phases:
        raise Denied(f"{tool.name} is not available in the {call['phase']} phase")
    if mode not in MODES:
        raise Denied(f"unknown mode: {mode}")
    try:
        prepared = tool.check(policy, call["args"]) if tool.check else None
    except PolicyError as error:
        raise Denied(f"denied by policy: {error}")
    return tool, prepared, needs_confirmation(tool.kind, mode)
