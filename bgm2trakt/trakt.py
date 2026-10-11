"""Trakt API client. Docs: https://trakt.docs.apiary.io/

Two login flows, depending on what kind of app Trakt issued:
- apps with a client secret use the device-code flow;
- public apps ("Not issued. This app signs users in with PKCE") use the
  authorization-code flow with PKCE and a localhost redirect.
"""
import base64
import hashlib
import json
import logging
import secrets
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

from .net import ApiError, request

API = "https://api.trakt.tv"
AUTHORIZE = "https://trakt.tv/oauth/authorize"
OOB = "urn:ietf:wg:oauth:2.0:oob"
LOCAL_REDIRECT = "http://localhost:8765/callback"
LOGIN_TIMEOUT = 300

log = logging.getLogger(__name__)

DEVICE_ERRORS = {404: "设备码无效", 409: "该代码已被使用", 410: "代码已过期", 418: "你拒绝了授权"}


class _Callback(BaseHTTPRequestHandler):
    """Catches the redirect back from trakt.tv/oauth/authorize."""

    def do_GET(self):
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        msg = ""  # favicon and other stray requests
        if "code" in query or "error" in query:
            self.server.query = query
            msg = "授权完成，可以关闭这个页面回到终端了。" if "code" in query else "授权失败，请回到终端查看。"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(f"<p>{msg}</p>".encode("utf-8"))

    def log_message(self, *args):
        pass


class Trakt:
    def __init__(self, client_id, client_secret, token_path, redirect_uri=""):
        if not client_id:
            raise SystemExit("config.json 里缺少 trakt.client_id")
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri or (OOB if client_secret else LOCAL_REDIRECT)
        self.token_path = token_path
        self.token = json.loads(token_path.read_text("utf-8")) if token_path.exists() else None
        self._last_get = 0.0
        self._last_post = 0.0
        self._seasons = {}

    # --- auth -------------------------------------------------------------

    def _save_token(self, token):
        self.token = token
        self.token_path.write_text(json.dumps(token, indent=2), "utf-8")

    def _client_auth(self):
        auth = {"client_id": self.client_id}
        if self.client_secret:
            auth["client_secret"] = self.client_secret
        return auth

    def login(self):
        log.debug("登录方式：%s，redirect_uri=%s", "设备码" if self.client_secret else "PKCE", self.redirect_uri)
        if self.client_secret:
            self.device_login()
        else:
            self.pkce_login()

    def device_login(self):
        code = request("POST", API + "/oauth/device/code", body={"client_id": self.client_id})
        log.info(f"请在浏览器打开 {code['verification_url']} 并输入代码：{code['user_code']}")
        deadline = time.monotonic() + code["expires_in"]
        interval = code["interval"]
        while time.monotonic() < deadline:
            time.sleep(interval)
            try:
                token = request(
                    "POST",
                    API + "/oauth/device/token",
                    body={"code": code["device_code"], **self._client_auth()},
                    retries=0,
                )
            except ApiError as e:
                if e.status == 400:  # still pending
                    continue
                if e.status == 429:  # polling too fast
                    interval += 1
                    continue
                raise SystemExit(DEVICE_ERRORS.get(e.status, str(e)))
            self._save_token(token)
            return
        raise SystemExit("授权超时，请重新运行 auth")

    def pkce_login(self):
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        state = secrets.token_urlsafe(16)
        url = AUTHORIZE + "?" + urllib.parse.urlencode({
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        })
        query = self._receive_callback(url, state)
        if "error" in query:
            raise SystemExit(f"授权失败：{query['error'][0]}")
        if query.get("state", [""])[0] != state:
            raise SystemExit("授权失败：state 不匹配，请重新运行 auth")
        try:
            token = request("POST", API + "/oauth/token", body={
                "code": query["code"][0],
                "client_id": self.client_id,
                "redirect_uri": self.redirect_uri,
                "grant_type": "authorization_code",
                "code_verifier": verifier,
            })
        except ApiError as e:
            raise SystemExit(f"换取令牌失败：{e}")
        self._save_token(token)

    def _receive_callback(self, url, state):
        """Send the user to the authorize page and return the redirect's query parameters."""
        target = urllib.parse.urlparse(self.redirect_uri)
        if target.scheme != "http" or target.hostname not in ("localhost", "127.0.0.1"):
            # e.g. the OOB redirect: Trakt shows the code on the page instead of redirecting.
            log.info(f"请在浏览器打开下面的网址并授权：\n{url}")
            code = input("然后把页面上显示的授权码粘贴到这里：").strip()
            return {"code": [code], "state": [state]}

        try:
            server = HTTPServer((target.hostname, target.port or 80), _Callback)
        except OSError as e:
            raise SystemExit(f"无法监听 {self.redirect_uri}（{e}），可以在 config.json 里把 trakt.redirect_uri 换个端口")
        server.query = None
        server.timeout = 1
        log.info(f"正在打开浏览器授权。如果没有自动打开，请手动访问：\n{url}")
        webbrowser.open(url)
        deadline = time.monotonic() + LOGIN_TIMEOUT
        with server:
            while server.query is None:
                if time.monotonic() > deadline:
                    raise SystemExit("授权超时，请重新运行 auth")
                server.handle_request()
        return server.query

    def _ensure_token(self):
        if not self.token:
            raise SystemExit("还没有登录 Trakt，请先运行：python -m bgm2trakt auth")
        expires = self.token["created_at"] + self.token["expires_in"]
        if time.time() > expires - 86400:
            log.debug("Trakt 令牌即将过期，正在续期")
            self._save_token(request(
                "POST",
                API + "/oauth/token",
                body={
                    "refresh_token": self.token["refresh_token"],
                    **self._client_auth(),
                    "redirect_uri": self.redirect_uri,
                    "grant_type": "refresh_token",
                },
            ))

    # --- transport ----------------------------------------------------------

    def _headers(self, auth):
        h = {"trakt-api-version": "2", "trakt-api-key": self.client_id}
        if auth:
            self._ensure_token()
            h["Authorization"] = f"Bearer {self.token['access_token']}"
        return h

    def get(self, path, params=None, auth=False, with_headers=False):
        # Trakt allows 1000 GETs per 5 minutes.
        wait = self._last_get + 0.35 - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last_get = time.monotonic()
        return request("GET", API + path, params=params, headers=self._headers(auth), with_headers=with_headers)

    def get_all(self, path, params=None, auth=False):
        """GET every page of a paginated endpoint (Trakt caps pages at 100 items)."""
        items, page = [], 1
        while True:
            rows, headers = self.get(path, {**(params or {}), "page": page, "limit": 100}, auth, with_headers=True)
            items += rows or []
            pages = int(headers.get("X-Pagination-Page-Count") or 1)
            log.debug("%s 第 %d/%d 页，%d 条", path, page, pages, len(rows or []))
            if page >= pages:
                return items
            page += 1

    def post(self, path, body):
        # Trakt allows 1 authenticated POST per second.
        wait = self._last_post + 1.1 - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        try:
            return request("POST", API + path, body=body, headers=self._headers(True))
        finally:
            self._last_post = time.monotonic()

    # --- endpoints ----------------------------------------------------------

    def search(self, query, types="show,movie"):
        return self.get(f"/search/{types}", {"query": query, "extended": "full", "limit": 10})

    def lookup_tmdb(self, tmdb_id, kind):
        return self.get(f"/search/tmdb/{tmdb_id}", {"type": kind, "extended": "full"})

    def summary(self, kind, id_or_slug):
        return self.get(f"/{kind}s/{id_or_slug}", {"extended": "full"})

    def seasons(self, show_id):
        if show_id not in self._seasons:
            self._seasons[show_id] = self.get(f"/shows/{show_id}/seasons", {"extended": "full,episodes"})
        return self._seasons[show_id]

    def watched(self, kind):
        return self.get_all(f"/sync/watched/{kind}", auth=True)

    def watched_episodes(self):
        """{show trakt id: {(season, episode), ...}} for every episode in the user's history.

        /sync/watched/shows no longer includes per-episode seasons, so read the history instead.
        """
        out = {}
        for h in self.get_all("/sync/history/episodes", auth=True):
            ep = h["episode"]
            out.setdefault(h["show"]["ids"]["trakt"], set()).add((ep["season"], ep["number"]))
        return out
