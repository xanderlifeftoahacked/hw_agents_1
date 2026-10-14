NAMES = ("model_requests", "tool_calls", "side_effects", "result_bytes")


class Exhausted(Exception):
    pass


def new(limits: dict) -> dict:
    return {"limits": {name: int(limits[name]) for name in NAMES}, "used": {name: 0 for name in NAMES}}


def remaining(run: dict, name: str) -> int:
    budget = run["budget"]
    return budget["limits"][name] - budget["used"][name]


def spend(run: dict, name: str, amount: int = 1) -> None:
    if remaining(run, name) < amount:
        used, limit = run["budget"]["used"][name], run["budget"]["limits"][name]
        raise Exhausted(f"{name}: used {used} of {limit}, need {amount} more")
    run["budget"]["used"][name] += amount


def first_missing(run: dict, needs: dict) -> str | None:
    return next((name for name, amount in needs.items() if remaining(run, name) < amount), None)


def output_limit(run: dict, cap: int) -> int:
    left = remaining(run, "result_bytes")
    if left <= 0:
        raise Exhausted(f"result_bytes: used {run['budget']['used']['result_bytes']} of {run['budget']['limits']['result_bytes']}")
    return min(cap, left)


def clip(text: str, limit: int) -> tuple[str, int, bool]:
    data = text.encode()
    if len(data) <= limit:
        return text, len(data), False
    kept = data[:limit].decode("utf-8", errors="ignore")
    return kept + f"\n[truncated: output was cut to {limit} bytes, the result is incomplete]", limit, True


def render(run: dict) -> str:
    budget = run["budget"]
    return ", ".join(f"{name} {budget['used'][name]}/{budget['limits'][name]}" for name in NAMES)
