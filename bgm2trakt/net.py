"""Minimal JSON-over-HTTP helper (stdlib only)."""
import json
import time
import urllib.error
import urllib.parse
import urllib.request

USER_AGENT = "bangumi2trakt/0.1 (+https://github.com/yohaneowo/bangumi2trakt)"


class ApiError(Exception):
    def __init__(self, status, url, body):
        super().__init__(f"HTTP {status} {url}: {body[:300]}")
        self.status = status
        self.body = body


def _retry_after(headers, attempt):
    try:
        return float(headers.get("Retry-After"))
    except (TypeError, ValueError):
        return 2 ** attempt


def request(method, url, *, params=None, headers=None, body=None, retries=4):
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    data = None if body is None else json.dumps(body).encode("utf-8")
    hdrs = {"Accept": "application/json", "User-Agent": USER_AGENT}
    if data is not None:
        hdrs["Content-Type"] = "application/json"
    hdrs.update(headers or {})

    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
                return json.loads(raw) if raw.strip() else None
        except urllib.error.HTTPError as e:
            text = e.read().decode("utf-8", "replace")
            if attempt < retries and (e.code == 429 or e.code >= 500):
                time.sleep(_retry_after(e.headers, attempt))
                continue
            raise ApiError(e.code, url, text) from None
        except (urllib.error.URLError, TimeoutError):
            if attempt < retries:
                time.sleep(2 ** attempt)
                continue
            raise
