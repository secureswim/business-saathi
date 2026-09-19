"""One tool-calling interface, three providers behind it.

The agent loop should not care which model it is talking to, so everything a
provider has to do is expressed as a single call:

    chat(messages, tools) -> {"calls": [{name, arguments, id}], "text": str}

OpenAI and NVIDIA NIM both speak the OpenAI tool-calling protocol natively.
Gemini speaks its own, and is translated here. A provider that cannot do tool
calling at all would still work through `emulated` mode, where the tool list is
described in the prompt and the model replies with a JSON object -- that path
exists so a strong Indic model can be dropped in without needing function
calling support.

Nothing here decides what to do; it only carries messages back and forth.
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import config  # noqa: E402

# A provider that fails is stood down briefly rather than retried into the
# ground; a quota or schema error is a longer stand-down than an overload.
_cooldown: dict[str, float] = {}
SHORT_COOLDOWN = 20.0
LONG_COOLDOWN = 300.0


class ProviderError(RuntimeError):
    pass


def _cooled(name: str) -> bool:
    return _cooldown.get(name, 0.0) > time.time()


def _stand_down(name: str, exc: Exception) -> None:
    text = str(exc).lower()
    hard = any(w in text for w in ("quota", "429", "invalid_api_key", "401", "403",
                                   "schema", "not found", "404"))
    _cooldown[name] = time.time() + (LONG_COOLDOWN if hard else SHORT_COOLDOWN)


def reset_cooldowns() -> None:
    _cooldown.clear()


def order() -> list[str]:
    """Which providers to try, in order, for this configuration."""
    ready = {"openai": config.openai_ready(), "gemini": config.gemini_ready(),
             "nvidia-nim": config.nvidia_ready()}
    choice = config.LLM_PROVIDER
    if choice in ready:
        return [choice] if ready[choice] else []
    if choice == "nvidia-first":
        names = ["nvidia-nim", "openai", "gemini"]
    elif choice == "gemini-first":
        names = ["gemini", "openai", "nvidia-nim"]
    else:                                   # auto: OpenAI leads
        names = ["openai", "gemini", "nvidia-nim"]
    return [n for n in names if ready[n]]


def available() -> bool:
    return bool([n for n in order() if not _cooled(n)])


# --------------------------------------------------------------- OpenAI wire
def _openai_tools(tools: list[dict]) -> list[dict]:
    return [{"type": "function",
             "function": {"name": t["name"], "description": t["description"],
                          "parameters": t["parameters"]}}
            for t in tools]


def _openai_chat(messages: list[dict], tools: list[dict], base_url: str, key: str,
                 model: str, timeout: float, force_json: bool) -> dict:
    import httpx

    body: dict = {"model": model, "messages": messages}
    if tools:
        body["tools"] = _openai_tools(tools)
        body["tool_choice"] = "auto"
    if force_json and not tools:
        body["response_format"] = {"type": "json_object"}
    r = httpx.post(f"{base_url}/chat/completions",
                   headers={"Authorization": f"Bearer {key}",
                            "Content-Type": "application/json"},
                   json=body, timeout=timeout)
    if r.status_code >= 400:
        raise ProviderError(f"HTTP {r.status_code}: {r.text[:400]}")
    message = r.json()["choices"][0]["message"]
    calls = []
    for c in message.get("tool_calls") or []:
        fn = c.get("function", {})
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except json.JSONDecodeError:
            args = {}
        calls.append({"id": c.get("id"), "name": fn.get("name"),
                      "arguments": args if isinstance(args, dict) else {}})
    return {"calls": calls, "text": message.get("content") or "",
            "raw_message": message}


# --------------------------------------------------------------- Gemini wire
def _gemini_clean(value):
    """Strip the JSON Schema keys the v1beta endpoint rejects."""
    if isinstance(value, dict):
        return {k: _gemini_clean(v) for k, v in value.items()
                if k not in {"additionalProperties"}}
    if isinstance(value, list):
        return [_gemini_clean(v) for v in value]
    return value


def _gemini_chat(messages: list[dict], tools: list[dict], timeout: float) -> dict:
    import httpx

    system = "\n".join(m["content"] for m in messages if m["role"] == "system")
    contents = []
    for m in messages:
        if m["role"] == "system":
            continue
        if m["role"] == "tool":
            contents.append({"role": "user", "parts": [{"functionResponse": {
                "name": m.get("name", "tool"),
                "response": {"result": m["content"]}}}]})
        elif m["role"] == "assistant" and m.get("tool_calls"):
            contents.append({"role": "model", "parts": [
                {"functionCall": {"name": c["name"], "args": c["arguments"]}}
                for c in m["tool_calls"]]})
        else:
            contents.append({"role": "model" if m["role"] == "assistant" else "user",
                             "parts": [{"text": m.get("content") or ""}]})

    body: dict = {"systemInstruction": {"parts": [{"text": system}]},
                  "contents": contents,
                  "generationConfig": {"maxOutputTokens": 1400,
                                       "thinkingConfig": {"thinkingLevel": "minimal"}}}
    if tools:
        body["tools"] = [{"functionDeclarations": [
            {"name": t["name"], "description": t["description"],
             "parameters": _gemini_clean(t["parameters"])} for t in tools]}]
    else:
        body["generationConfig"]["responseMimeType"] = "application/json"

    r = httpx.post("https://generativelanguage.googleapis.com/v1beta/models/"
                   f"{config.GEMINI_MODEL}:generateContent",
                   headers={"x-goog-api-key": config.GEMINI_API_KEY,
                            "Content-Type": "application/json"},
                   json=body, timeout=timeout)
    if r.status_code >= 400:
        raise ProviderError(f"HTTP {r.status_code}: {r.text[:400]}")
    parts = r.json().get("candidates", [{}])[0].get("content", {}).get("parts", [])
    calls, text = [], ""
    for part in parts:
        if not isinstance(part, dict):
            continue
        if "functionCall" in part:
            fc = part["functionCall"]
            calls.append({"id": fc.get("name"), "name": fc.get("name"),
                          "arguments": fc.get("args") or {}})
        elif "text" in part:
            text += part["text"]
    return {"calls": calls, "text": text.strip(), "raw_message": None}


# ------------------------------------------------------------------- facade
def chat(messages: list[dict], tools: list[dict] | None = None,
         timeout: float = 9.0, force_json: bool = False,
         provider: str | None = None) -> dict:
    """Try each configured provider in turn. Raises only if all of them fail."""
    names = [provider] if provider else order()
    names = [n for n in names if not _cooled(n)] or names
    if not names:
        raise ProviderError("no LLM provider is configured")

    last = None
    for name in names:
        try:
            if name == "openai":
                out = _openai_chat(messages, tools or [], config.OPENAI_BASE_URL,
                                   config.OPENAI_API_KEY, config.OPENAI_MODEL,
                                   timeout, force_json)
            elif name == "nvidia-nim":
                out = _openai_chat(messages, tools or [], config.NVIDIA_BASE_URL,
                                   config.NVIDIA_API_KEY, config.NVIDIA_MODEL,
                                   timeout, force_json)
            elif name == "gemini":
                out = _gemini_chat(messages, tools or [], timeout)
            else:
                continue
            out["provider"] = name
            return out
        except Exception as exc:                      # noqa: BLE001
            _stand_down(name, exc)
            last = exc
    raise ProviderError(f"all providers failed; last error: {last}")


def speech_provider() -> str | None:
    """Which provider voices the answer. 'same' means the reasoning provider."""
    if config.SPEECH_PROVIDER in ("same", "", None):
        return None
    return config.SPEECH_PROVIDER


def parse_json(text: str) -> dict:
    """Best-effort JSON out of a model's text reply."""
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-z]*\s*|\s*```$", "", text, flags=re.I | re.S)
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.S)
        if not match:
            raise ValueError(f"no JSON object in model reply: {text[:200]}")
        value = json.loads(match.group(0))
    if not isinstance(value, dict):
        raise ValueError("model returned a non-object")
    return value
