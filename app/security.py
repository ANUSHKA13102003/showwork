import time
from collections import defaultdict, deque
from threading import Lock

from fastapi import HTTPException, Request

from app.config import flag, setting

MAX_QUESTION_LEN = 400

CSP = (
    "default-src 'self'; "
    "script-src 'self' https://cdn.jsdelivr.net; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src 'self' https://fonts.gstatic.com; "
    "img-src 'self' data:; "
    "connect-src 'self'; "
    "frame-ancestors 'none'; "
    "base-uri 'self'; "
    "form-action 'self'"
)


class RateLimiter:
    def __init__(self):
        self.lock = Lock()
        self.hits = defaultdict(deque)

    def limit(self):
        return setting("RATE_LIMIT_PER_MIN", 30)

    def check(self, key, window=60.0):
        now = time.monotonic()
        cap = self.limit()
        with self.lock:
            q = self.hits[key]
            while q and now - q[0] > window:
                q.popleft()
            if len(q) >= cap:
                return False
            q.append(now)
            return True

    def reset(self):
        with self.lock:
            self.hits.clear()


limiter = RateLimiter()


def client_ip(request: Request) -> str:
    if flag("TRUST_PROXY", "0"):
        xff = request.headers.get("x-forwarded-for", "")
        if xff:
            first = xff.split(",")[0].strip()
            if first:
                return first
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


def enforce_rate_limit(request: Request):
    ip = client_ip(request)
    if not limiter.check(ip):
        raise HTTPException(
            status_code=429,
            detail="Rate limit exceeded. Please wait a minute before sending more requests.",
        )


def validate_question(text: str) -> str:
    if not isinstance(text, str):
        raise HTTPException(status_code=400, detail="Question must be plain text.")
    cleaned = text.strip()
    if not cleaned:
        raise HTTPException(status_code=400, detail="Enter a question first.")
    if len(cleaned) > MAX_QUESTION_LEN:
        raise HTTPException(
            status_code=400,
            detail=f"Question is too long ({len(cleaned)} characters; max {MAX_QUESTION_LEN}).",
        )
    return cleaned


def add_security_headers(response):
    response.headers["Content-Security-Policy"] = CSP
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    return response
