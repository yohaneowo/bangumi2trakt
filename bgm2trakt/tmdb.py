"""Optional TMDB search. TMDB handles Japanese/Chinese titles better than Trakt's own search."""
from .net import request

API = "https://api.themoviedb.org/3"


class TMDB:
    def __init__(self, key):
        # Accept either a v3 API key or a v4 "API Read Access Token" (a JWT).
        if key.startswith("eyJ"):
            self.headers, self.params = {"Authorization": f"Bearer {key}"}, {}
        else:
            self.headers, self.params = {}, {"api_key": key}

    def search(self, query, kind):
        """kind: 'tv' or 'movie'."""
        res = request(
            "GET",
            f"{API}/search/{kind}",
            params={**self.params, "query": query, "language": "zh-CN"},
            headers=self.headers,
        )
        return res.get("results", [])
