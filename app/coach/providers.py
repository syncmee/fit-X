"""Provider transport kept separate from dashboard routes.

Dependencies are looked up from ``app.dashboard`` at call time during the
compatibility period.  That avoids an import cycle while preserving tests that
patch ``app.dashboard._gemini_generate``.
"""
from __future__ import annotations

import requests


GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
GEMINI_TIMEOUT_SECONDS = 12
AI_TIMEOUT_SECONDS = 45


def gemini_generate(user, message: str, now):
    from app import dashboard as dashboard

    api_key = dashboard.current_app.config.get("GEMINI_API_KEY", "")
    model = dashboard.current_app.config.get("GEMINI_MODEL", "gemini-2.5-flash")
    contents = [
        {"role": "user" if row.role == "user" else "model", "parts": [{"text": row.content}]}
        for row in reversed(dashboard._ai_recent_history(user))
    ] + [{"role": "user", "parts": [{"text": message}]}]
    payload = {"system_instruction": {"parts": [{"text": dashboard._ai_system_prompt(user, now, message)}]},
               "contents": contents,
               "generationConfig": {"temperature": 0.4, "maxOutputTokens": 800, "responseMimeType": "application/json"}}
    try:
        response = requests.post(GEMINI_API_URL.format(model=model), headers={"x-goog-api-key": api_key, "Content-Type": "application/json"}, json=payload, timeout=GEMINI_TIMEOUT_SECONDS)
        if response.status_code == 503:
            dashboard.sleep(2)
            response = requests.post(GEMINI_API_URL.format(model=model), headers={"x-goog-api-key": api_key, "Content-Type": "application/json"}, json=payload, timeout=GEMINI_TIMEOUT_SECONDS)
    except requests.RequestException as exc:
        raise dashboard.CoachAIError(f"Gemini request failed: {exc}") from exc
    if response.status_code != 200:
        raise dashboard.CoachAIError(f"Gemini returned {response.status_code}: {response.text[:300]}")
    try:
        text = response.json()["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError, ValueError) as exc:
        raise dashboard.CoachAIError(f"Gemini response shape unexpected: {response.text[:300]}") from exc
    result = dashboard._parse_coach_json(text, "Gemini")
    usage = response.json().get("usageMetadata") or {}
    result["usage"] = {"prompt": usage.get("promptTokenCount"), "completion": usage.get("candidatesTokenCount")}
    return result


def groq_generate(user, message: str, now):
    from app import dashboard as dashboard

    api_key = dashboard.current_app.config.get("GROQ_API_KEY", "")
    model = dashboard.current_app.config.get("GROQ_MODEL", "openai/gpt-oss-120b")
    messages = [{"role": "system", "content": dashboard._ai_system_prompt(user, now, message)}]
    messages.extend({"role": "user" if row.role == "user" else "assistant", "content": row.content}
                    for row in reversed(dashboard._ai_recent_history(user)))
    messages.append({"role": "user", "content": message})
    try:
        response = requests.post(GROQ_API_URL, headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}, json={"model": model, "messages": messages, "temperature": 0.4, "max_tokens": 1000, "response_format": {"type": "json_object"}}, timeout=AI_TIMEOUT_SECONDS)
    except requests.RequestException as exc:
        raise dashboard.CoachAIError(f"Groq request failed: {exc}") from exc
    if response.status_code != 200:
        raise dashboard.CoachAIError(f"Groq returned {response.status_code}: {response.text[:300]}")
    try:
        text = response.json()["choices"][0]["message"]["content"]
    except (KeyError, IndexError, ValueError) as exc:
        raise dashboard.CoachAIError(f"Groq response shape unexpected: {response.text[:300]}") from exc
    result = dashboard._parse_coach_json(text, "Groq")
    usage = response.json().get("usage") or {}
    result["usage"] = {"prompt": usage.get("prompt_tokens"), "completion": usage.get("completion_tokens")}
    return result
