import copy
import json
import os
import re
import uuid
from dataclasses import dataclass, field

THINK = re.compile(r"<think>.*?</think>", re.DOTALL)


@dataclass
class Reply:
    content: str = ""
    tool_calls: list[dict] = field(default_factory=list)


class OpenAIModel:
    def __init__(self):
        from openai import OpenAI

        self.model = os.environ.get("OPENAI_MODEL", "qwen3:4b")
        self.client = OpenAI(
            base_url=os.environ.get("OPENAI_BASE_URL", "http://localhost:11434/v1/"),
            api_key=os.environ.get("OPENAI_API_KEY", "ollama"),
            timeout=float(os.environ.get("OPENAI_TIMEOUT", "600")),
        )

    def complete(self, messages: list[dict], tools: list[dict]) -> Reply:
        response = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            tools=tools,
            temperature=0,
        )
        message = response.choices[0].message
        calls = [
            {
                "id": call.id or f"call_{uuid.uuid4().hex[:12]}",
                "name": call.function.name,
                "arguments": call.function.arguments or "{}",
            }
            for call in message.tool_calls or []
        ]
        return Reply(THINK.sub("", message.content or "").strip(), calls)


class ScriptedModel:
    def __init__(self, replies: list[Reply]):
        self.replies = list(replies)
        self.requests = []

    def complete(self, messages: list[dict], tools: list[dict]) -> Reply:
        self.requests.append({"messages": copy.deepcopy(messages), "tools": [t["function"]["name"] for t in tools]})
        if not self.replies:
            raise RuntimeError("scripted model has no more replies")
        return self.replies.pop(0)


def call(name: str, call_id: str, **args) -> dict:
    return {"id": call_id, "name": name, "arguments": json.dumps(args)}
