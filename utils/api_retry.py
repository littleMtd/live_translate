"""Shared API error classification and retry configuration."""

_RETRY_DELAYS: tuple[int, ...] = (1, 3, 7)  # seconds between attempts — for network errors only
# Rate limits are NOT retried in the translator (triggers fallback immediately).
# _RETRY_DELAYS is kept for background, non-blocking network-error retries.


def classify_error(e: Exception) -> str:
    """Return broad error category: 'rate_limit' | 'network' | 'auth' | 'other'.

    Uses class name and message text so it works whether the real SDK or a mock
    raises the exception.
    """
    name = type(e).__name__
    msg = str(e).lower()

    if any(x in name for x in ("RateLimit", "ResourceExhausted", "QuotaExceeded")):
        return "rate_limit"
    if "429" in msg or ("rate" in msg and "limit" in msg) or "quota" in msg:
        return "rate_limit"

    if any(x in name for x in ("Connection", "Timeout", "ServiceUnavailable",
                                "Unavailable", "Deadline")):
        return "network"
    if "timeout" in msg or "500" in msg or "502" in msg or "503" in msg:
        return "network"

    if any(x in name for x in ("Authentication", "Permission", "Forbidden",
                                "Unauthorized")):
        return "auth"
    if "401" in msg or "403" in msg or "authentication" in msg or "api key" in msg:
        return "auth"

    return "other"
