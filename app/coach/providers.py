"""Provider transport kept separate from dashboard routes.

Dependencies are looked up from ``app.dashboard`` at call time during the
compatibility period.  That avoids an import cycle while preserving tests that
patch ``app.dashboard._gemini_generate``.
"""
from __future__ import annotations

from time import monotonic

import requests

from app.coach import CoachAIError
from app.coach import prompt as prompt_module


# One HTTP session across calls: TLS/handshake reuse saves ~100-300 ms per
# coach turn, and pooled connections survive across requests.
_SESSION = requests.Session()

GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
GEMINI_TIMEOUT_SECONDS = 12
AI_TIMEOUT_SECONDS = 45
# Wall-clock budget shared by the whole Gemini -> Groq fallback chain, so a
# hung Gemini cannot push the worst-case coach response to
# GEMINI_TIMEOUT + AI_TIMEOUT (57s). When the deadline is exhausted the
# caller falls through to the rule-based coach instead of waiting more.
TOTAL_AI_DEADLINE_SECONDS = 25
MIN_PROVIDER_TIMEOUT_SECONDS = 2.0


def remaining_provider_timeout(deadline: float | None, default: float) -> float:
    """Per-request timeout capped by the chain-wide remaining deadline."""
    if deadline is None:
        return default
    remaining = deadline - monotonic()
    if remaining < MIN_PROVIDER_TIMEOUT_SECONDS:
        return 0.0
    return min(default, remaining)


def _deadline_check(deadline: float | None, provider: str) -> None:
    from app import dashboard as dashboard

    if deadline is not None and deadline - monotonic() < MIN_PROVIDER_TIMEOUT_SECONDS:
        raise CoachAIError(f"AI deadline exhausted before {provider}")


def gemini_generate(user, message: str, now, deadline: float | None = None):
    from app import dashboard as dashboard

    _deadline_check(deadline, "Gemini")
    timeout = remaining_provider_timeout(deadline, GEMINI_TIMEOUT_SECONDS)

    api_key = dashboard.current_app.config.get("GEMINI_API_KEY", "")
    model = dashboard.current_app.config.get("GEMINI_MODEL", "gemini-2.5-flash")
    contents = [
        {"role": "user" if row.role == "user" else "model", "parts": [{"text": row.content}]}
        for row in reversed(prompt_module.recent_history(user))
    ] + [{"role": "user", "parts": [{"text": message}]}]
    payload = {"system_instruction": {"parts": [{"text": prompt_module.build_system_prompt(user, now, message)}]},
               "contents": contents,
               "generationConfig": {"temperature": 0.4, "maxOutputTokens": 800, "responseMimeType": "application/json"}}
    try:
        response = _SESSION.post(GEMINI_API_URL.format(model=model), headers={"x-goog-api-key": api_key, "Content-Type": "application/json"}, json=payload, timeout=timeout)
        if response.status_code == 503 and remaining_provider_timeout(deadline, GEMINI_TIMEOUT_SECONDS) > 0:
            dashboard.sleep(2)
            timeout = remaining_provider_timeout(deadline, GEMINI_TIMEOUT_SECONDS)
            response = _SESSION.post(GEMINI_API_URL.format(model=model), headers={"x-goog-api-key": api_key, "Content-Type": "application/json"}, json=payload, timeout=timeout)
    except requests.RequestException as exc:
        raise CoachAIError(f"Gemini request failed: {exc}") from exc
    if response.status_code != 200:
        raise CoachAIError(f"Gemini returned {response.status_code}: {response.text[:300]}")
    try:
        text = response.json()["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError, ValueError) as exc:
        raise CoachAIError(f"Gemini response shape unexpected: {response.text[:300]}") from exc
    result = prompt_module.parse_coach_json(text, "Gemini")
    usage = response.json().get("usageMetadata") or {}
    result["usage"] = {"prompt": usage.get("promptTokenCount"), "completion": usage.get("candidatesTokenCount")}
    return result


def groq_generate(user, message: str, now, deadline: float | None = None):
    from app import dashboard as dashboard

    _deadline_check(deadline, "Groq")
    timeout = remaining_provider_timeout(deadline, AI_TIMEOUT_SECONDS)

    api_key = dashboard.current_app.config.get("GROQ_API_KEY", "")
    model = dashboard.current_app.config.get("GROQ_MODEL", "openai/gpt-oss-120b")
    messages = [{"role": "system", "content": prompt_module.build_system_prompt(user, now, message)}]
    messages.extend({"role": "user" if row.role == "user" else "assistant", "content": row.content}
                    for row in reversed(prompt_module.recent_history(user)))
    messages.append({"role": "user", "content": message})
    try:
        response = _SESSION.post(GROQ_API_URL, headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}, json={"model": model, "messages": messages, "temperature": 0.4, "max_tokens": 600, "response_format": {"type": "json_object"}}, timeout=timeout)
    except requests.RequestException as exc:
        raise CoachAIError(f"Groq request failed: {exc}") from exc
    if response.status_code != 200:
        raise CoachAIError(f"Groq returned {response.status_code}: {response.text[:300]}")
    try:
        text = response.json()["choices"][0]["message"]["content"]
    except (KeyError, IndexError, ValueError) as exc:
        raise CoachAIError(f"Groq response shape unexpected: {response.text[:300]}") from exc
    result = prompt_module.parse_coach_json(text, "Groq")
    usage = response.json().get("usage") or {}
    result["usage"] = {"prompt": usage.get("prompt_tokens"), "completion": usage.get("completion_tokens")}
    return result
