"""Minimal JSON-over-HTTP helper (stdlib only)."""
import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request

USER_AGENT = "bangumi2trakt/0.1 (+https://github.com/yohaneowo/bangumi2trakt)"

log = logging.getLogger(__name__)


class ApiError(Exception):
    def __init__(self, status, url, body):
        super().__init__(f"HTTP {status} {redact(url)}: {body[:300]}")
        self.status = status
        self.body = body


def redact(url):
    """Hide credentials that travel in query strings (TMDB's api_key) before logging."""
    return re.sub(r"(api_key=)[^&]+", r"\1***", url)


def _retry_after(headers, attempt):
    try:
        return float(headers.get("Retry-After"))
    except (TypeError, ValueError):
        return 2 ** attempt


def request(method, url, *, params=None, headers=None, body=None, retries=4, with_headers=False):
    """Returns the decoded JSON body, or (body, response headers) when with_headers=True."""
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    data = None if body is None else json.dumps(body).encode("utf-8")
    hdrs = {"Accept": "application/json", "User-Agent": USER_AGENT}
    if data is not None:
        hdrs["Content-Type"] = "application/json"
    hdrs.update(headers or {})
    shown = redact(url)

    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
        start = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
                log.debug("%s %s -> %s (%.2fs, %d bytes)", method, shown, resp.status, time.monotonic() - start, len(raw))
                result = json.loads(raw) if raw.strip() else None
                return (result, resp.headers) if with_headers else result
        except urllib.error.HTTPError as e:
            text = e.read().decode("utf-8", "replace")
            log.debug("%s %s -> %s (%.2fs): %s", method, shown, e.code, time.monotonic() - start, text[:500])
            if attempt < retries and (e.code == 429 or e.code >= 500):
                wait = _retry_after(e.headers, attempt)
                log.warning("HTTP %s，%.0f 秒后重试（第 %d 次）：%s %s", e.code, wait, attempt + 1, method, shown)
                time.sleep(wait)
                continue
            raise ApiError(e.code, url, text) from None
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt < retries:
                log.warning("网络错误（%s），%d 秒后重试（第 %d 次）：%s %s", e, 2 ** attempt, attempt + 1, method, shown)
                time.sleep(2 ** attempt)
                continue
            log.error("网络错误，已放弃：%s %s（%s）", method, shown, e)
            raise
