import json
import logging
import os
import threading
import time
from datetime import UTC, datetime

import httpx

from app.config import flag, setting

log = logging.getLogger("showwork.llm")

API = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")


class Budget:
    def __init__(self):
        self.lock = threading.Lock()
        self.day = None
        self.used = 0

    def cap(self):
        return setting("LLM_DAILY_CAP", 150)

    def _roll(self):
        today = datetime.now(UTC).date()
        if self.day != today:
            self.day = today
            self.used = 0

    def take(self):
        with self.lock:
            self._roll()
            if self.used >= self.cap():
                return False
            self.used += 1
            return True

    def remaining(self):
        with self.lock:
            self._roll()
            return max(self.cap() - self.used, 0)


budget = Budget()


def has_key():
    return bool(os.getenv("GEMINI_API_KEY", "").strip())


def mode():
    if not has_key():
        return {"mode": "no-key", "reason": "No Gemini key is configured."}
    if not flag("LLM_ENABLED", "1"):
        return {"mode": "no-key", "reason": "The LLM is switched off on this server."}
    if budget.remaining() <= 0:
        return {"mode": "no-key", "reason": "Today's LLM call limit is used up. It resets at midnight UTC."}
    return {"mode": "llm", "reason": "Gemini maps questions to defined metrics. Code computes every number."}


def _text(payload):
    parts = payload.get("candidates", [{}])[0].get("content", {}).get("parts", [])
    return "".join(p.get("text", "") for p in parts if not p.get("thought"))


def generate(prompt, system=None, schema=None, temperature=0.0, metered=True, retries=3):
    """Return the model's text, or None when the LLM is unavailable for any reason."""
    if not has_key() or not flag("LLM_ENABLED", "1"):
        return None
    if metered and not budget.take():
        return None
    body = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": temperature},
    }
    if system:
        body["systemInstruction"] = {"parts": [{"text": system}]}
    if schema:
        body["generationConfig"]["responseMimeType"] = "application/json"
        body["generationConfig"]["responseSchema"] = schema
    headers = {"x-goog-api-key": os.environ["GEMINI_API_KEY"].strip()}
    url = API.format(model=MODEL)
    for attempt in range(retries):
        try:
            res = httpx.post(url, headers=headers, json=body, timeout=25)
        except httpx.HTTPError as exc:
            log.warning("llm request failed: %s", type(exc).__name__)
            time.sleep(2 * (attempt + 1))
            continue
        if res.status_code == 200:
            return _text(res.json())
        # status only; the response body can echo request details
        log.warning("llm status %s", res.status_code)
        if res.status_code in (429, 500, 503):
            time.sleep(4 * (attempt + 1))
            continue
        return None
    return None


def generate_json(prompt, system=None, schema=None, metered=True):
    text = generate(prompt, system=system, schema=schema, metered=metered)
    if text is None:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None
