"""Trakt API client with device-code OAuth. Docs: https://trakt.docs.apiary.io/"""
import json
import time

from .net import ApiError, request

API = "https://api.trakt.tv"
OOB = "urn:ietf:wg:oauth:2.0:oob"

DEVICE_ERRORS = {404: "设备码无效", 409: "该代码已被使用", 410: "代码已过期", 418: "你拒绝了授权"}


class Trakt:
    def __init__(self, client_id, client_secret, token_path):
        if not client_id or not client_secret:
            raise SystemExit("config.json 里缺少 trakt.client_id / trakt.client_secret")
        self.client_id = client_id
        self.client_secret = client_secret
        self.token_path = token_path
        self.token = json.loads(token_path.read_text("utf-8")) if token_path.exists() else None
        self._last_get = 0.0
        self._last_post = 0.0
        self._seasons = {}

    # --- auth -------------------------------------------------------------

    def _save_token(self, token):
        self.token = token
        self.token_path.write_text(json.dumps(token, indent=2), "utf-8")

    def device_login(self):
        code = request("POST", API + "/oauth/device/code", body={"client_id": self.client_id})
        print(f"请在浏览器打开 {code['verification_url']} 并输入代码：{code['user_code']}")
        deadline = time.monotonic() + code["expires_in"]
        interval = code["interval"]
        while time.monotonic() < deadline:
            time.sleep(interval)
            try:
                token = request(
                    "POST",
                    API + "/oauth/device/token",
                    body={
                        "code": code["device_code"],
                        "client_id": self.client_id,
                        "client_secret": self.client_secret,
                    },
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

    def _ensure_token(self):
        if not self.token:
            raise SystemExit("还没有登录 Trakt，请先运行：python -m bgm2trakt auth")
        expires = self.token["created_at"] + self.token["expires_in"]
        if time.time() > expires - 86400:
            self._save_token(request(
                "POST",
                API + "/oauth/token",
                body={
                    "refresh_token": self.token["refresh_token"],
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "redirect_uri": OOB,
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

    def get(self, path, params=None, auth=False):
        # Trakt allows 1000 GETs per 5 minutes.
        wait = self._last_get + 0.35 - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last_get = time.monotonic()
        return request("GET", API + path, params=params, headers=self._headers(auth))

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
        return self.get(f"/sync/watched/{kind}", auth=True)
