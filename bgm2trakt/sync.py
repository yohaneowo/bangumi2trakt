"""Turn Bangumi collections + mapping.json into Trakt history / ratings / watchlist payloads."""
from collections import defaultdict
from datetime import datetime, timezone

from .bangumi import COLLECTION_TYPES
from .matcher import episode_range


def to_trakt_time(iso):
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def fmt_range(eps):
    if not eps:
        return "无新集数"
    (s1, e1), (s2, e2) = eps[0], eps[-1]
    return f"S{s1}E{e1}" if len(eps) == 1 else f"S{s1}E{e1}–S{s2}E{e2} ({len(eps)}集)"


class Plan:
    def __init__(self):
        self.history_movies = {}                                      # tid -> watched_at
        self.history_shows = defaultdict(lambda: defaultdict(dict))   # tid -> season -> ep -> watched_at
        self.rate_movies = {}                                         # tid -> (rating, rated_at)
        self.rate_seasons = defaultdict(list)                         # (tid, season) -> [(rating, rated_at)]
        self.rate_shows = defaultdict(list)                           # tid -> [(rating, rated_at)]
        self.watchlist = {"movie": set(), "show": set()}
        self.lines = []

    @staticmethod
    def _avg(votes):
        return round(sum(r for r, _ in votes) / len(votes)), max(t for _, t in votes)

    def bodies(self):
        history = {
            "movies": [{"ids": {"trakt": t}, "watched_at": w} for t, w in self.history_movies.items()],
            "shows": [
                {"ids": {"trakt": t}, "seasons": [
                    {"number": s, "episodes": [{"number": e, "watched_at": w} for e, w in eps.items()]}
                    for s, eps in seasons.items()
                ]}
                for t, seasons in self.history_shows.items()
            ],
        }

        shows = defaultdict(dict)
        for tid, votes in self.rate_shows.items():
            r, at = self._avg(votes)
            shows[tid].update(ids={"trakt": tid}, rating=r, rated_at=at)
        for (tid, season), votes in self.rate_seasons.items():
            r, at = self._avg(votes)
            shows[tid].setdefault("ids", {"trakt": tid})
            shows[tid].setdefault("seasons", []).append({"number": season, "rating": r, "rated_at": at})
        ratings = {
            "movies": [{"ids": {"trakt": t}, "rating": r, "rated_at": at} for t, (r, at) in self.rate_movies.items()],
            "shows": list(shows.values()),
        }

        watchlist = {
            "movies": [{"ids": {"trakt": t}} for t in self.watchlist["movie"]],
            "shows": [{"ids": {"trakt": t}} for t in self.watchlist["show"]],
        }
        return {"history": history, "ratings": ratings, "watchlist": watchlist}


def build_plan(items, mapping, trakt, opts, include_uncertain):
    watched_movies = {m["movie"]["ids"]["trakt"] for m in trakt.watched("movies")}
    watched_eps = {
        s["show"]["ids"]["trakt"]: {(se["number"], e["number"]) for se in s.get("seasons", []) for e in se["episodes"]}
        for s in trakt.watched("shows")
    }

    plan = Plan()
    skipped = defaultdict(int)
    for it in items:
        e = mapping.get(str(it["subject_id"]))
        status = e["status"] if e else "unmatched"
        if status in ("unmatched", "not_found", "skip") or (status == "uncertain" and not include_uncertain):
            skipped[status] += 1
            continue
        tid, kind, ctype = e["trakt_id"], e["type"], it["type"]
        when = to_trakt_time(it["updated_at"])
        name = it["subject"].get("name_cn") or it["subject"]["name"]
        parts = []

        if ctype == 1:
            if opts["watchlist"]:
                plan.watchlist[kind].add(tid)
                parts.append("加入 watchlist")
        elif opts["history"] and ctype in opts["history_collection_types"]:
            watched_at = "released" if opts["watched_at"] == "released" else when
            if kind == "movie":
                if ctype == 2 and tid not in watched_movies:
                    plan.history_movies[tid] = watched_at
                    parts.append("标记看过")
            else:
                if ctype == 2:
                    count = e.get("episode_count") or it["subject"].get("eps") or None
                else:
                    count = it.get("ep_status") or 0
                if count != 0:
                    eps = episode_range(trakt.seasons(tid), e["season"], e["episode_start"], count)
                    new = [x for x in eps if x not in watched_eps.get(tid, ())]
                    for s, n in new:
                        plan.history_shows[tid][s].setdefault(n, watched_at)
                    parts.append(fmt_range(new) if eps else "!! 找不到对应集数，请检查 mapping")

        if opts["ratings"] and it.get("rate"):
            vote = (it["rate"], when)
            if kind == "movie":
                plan.rate_movies[tid] = vote
            else:
                if opts["rating_target"] in ("season", "both"):
                    plan.rate_seasons[(tid, e["season"])].append(vote)
                if opts["rating_target"] in ("show", "both"):
                    plan.rate_shows[tid].append(vote)
            parts.append(f"评分 {it['rate']}")

        if parts:
            target = e.get("trakt_title") or e.get("trakt_slug") or tid
            plan.lines.append(f"[{COLLECTION_TYPES[ctype]}] {name} → {target}: {', '.join(parts)}")
    return plan, skipped


def _chunks(body, size):
    """Split {"movies": [...], "shows": [...]} into several bodies of at most `size` items."""
    for key in ("movies", "shows"):
        rows = body.get(key) or []
        for i in range(0, len(rows), size):
            yield {key: rows[i:i + size]}


def apply_plan(trakt, bodies):
    for name, path in (("history", "/sync/history"), ("ratings", "/sync/ratings"), ("watchlist", "/sync/watchlist")):
        added = defaultdict(int)
        not_found = 0
        for chunk in _chunks(bodies[name], 50):
            res = trakt.post(path, chunk) or {}
            for k, v in (res.get("added") or {}).items():
                added[k] += v
            not_found += sum(len(v) for v in (res.get("not_found") or {}).values() if isinstance(v, list))
        summary = ", ".join(f"{k} {v}" for k, v in added.items() if v) or "无"
        print(f"{name}: 新增 {summary}" + (f"，Trakt 找不到 {not_found} 项" if not_found else ""))
