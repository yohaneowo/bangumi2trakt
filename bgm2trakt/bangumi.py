"""Bangumi API v0 client. Docs: https://bangumi.github.io/api/"""
import urllib.parse

from .net import request

API = "https://api.bgm.tv"

COLLECTION_TYPES = {1: "想看", 2: "看过", 3: "在看", 4: "搁置", 5: "抛弃"}


class Bangumi:
    def __init__(self, access_token=""):
        self.headers = {"Authorization": f"Bearer {access_token}"} if access_token else {}

    def _get(self, path, params=None):
        return request("GET", API + path, params=params, headers=self.headers)

    def me(self):
        return self._get("/v0/me")

    def collections(self, username, subject_type):
        items, offset = [], 0
        while True:
            page = self._get(
                f"/v0/users/{urllib.parse.quote(username)}/collections",
                {"subject_type": subject_type, "limit": 50, "offset": offset},
            )
            items += page["data"]
            offset += len(page["data"])
            if not page["data"] or offset >= page["total"]:
                return items

    def subject(self, subject_id):
        return self._get(f"/v0/subjects/{subject_id}")
