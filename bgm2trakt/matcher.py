"""Map a Bangumi subject (one season / cour) onto a Trakt movie or a show + starting episode.

Bangumi splits every season (often every cour) into its own subject, while Trakt follows
TVDB/TMDB and has one show with seasons. We search Trakt by title, then use the subject's
air date to find the Trakt episode that premiered that day, which gives both the season
and the starting episode (this also handles split cours and absolute numbering).
"""
import re
from datetime import date, datetime, timedelta, timezone

from .net import ApiError

MOVIE_PLATFORMS = {"剧场版", "电影", "Movie", "movie"}
EITHER_PLATFORMS = {"OVA", "OAD", "WEB"}
EP_TOLERANCE_DAYS = 3
MAX_CANDIDATES = 12
JST = timezone(timedelta(hours=9))

SEASON_SUFFIXES = [
    r"\s*第\s*[0-9０-９一二三四五六七八九十]+\s*(?:期|季|部|章|クール|シーズン).*$",
    r"\s*(?:season|Season|SEASON)\s*\d+.*$",
    r"\s*\d+(?:st|nd|rd|th)\s*(?:season|Season|SEASON).*$",
    r"\s*(?:Part|PART|part)\.?\s*\d+.*$",
    r"\s*[(（][^()（）]*[)）]\s*$",
    r"\s+(?:II|III|IV|V|Ⅱ|Ⅲ|Ⅳ|Ⅴ|\d)$",
]


def parse_date(s):
    if not s:
        return None
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        return None


def aired_date(iso):
    """Trakt timestamps are UTC; anime air dates on Bangumi are Japan local time."""
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(JST).date()


def search_queries(subject):
    titles = [t for t in (subject.get("name"), subject.get("name_cn")) if t]
    out = list(dict.fromkeys(titles))
    for t in titles:
        base = t
        for pat in SEASON_SUFFIXES:
            base = re.sub(pat, "", base).strip() or base
        if base not in out:
            out.append(base)
        # "鬼滅の刃 遊郭編" -> "鬼滅の刃"
        head = re.split(r"[\s:：～~\-－!！]", t.strip())[0]
        if len(head) >= 2 and head not in out:
            out.append(head)
    return out


def wanted_kind(subject):
    platform = subject.get("platform") or ""
    if platform in MOVIE_PLATFORMS:
        return "movie"
    if platform in EITHER_PLATFORMS:
        return None
    return "show"


def locate_episode(seasons, target):
    """Find the episode that aired closest to `target`. Returns (season, episode) or None."""
    best = None
    for s in seasons:
        for ep in s.get("episodes") or []:
            if not ep.get("first_aired"):
                continue
            delta = abs((aired_date(ep["first_aired"]) - target).days)
            key = (delta, s["number"] == 0, s["number"], ep["number"])
            if best is None or key < best:
                best = key
    if best and best[0] <= EP_TOLERANCE_DAYS:
        return best[2], best[3]
    return None


def episode_range(seasons, season, start, count):
    """`count` consecutive episodes starting at (season, start).

    Regular seasons roll over into the next one when the overflow is large (a Bangumi
    subject may span two Trakt seasons); specials (season 0) never do.
    count=None means "rest of this season".
    """
    ordered = sorted(seasons, key=lambda s: s["number"])
    if season == 0:
        ordered = [s for s in ordered if s["number"] == 0]
    else:
        ordered = [s for s in ordered if s["number"] > 0]
    flat = [
        (s["number"], e["number"])
        for s in ordered
        for e in sorted(s.get("episodes") or [], key=lambda e: e["number"])
    ]
    try:
        i = flat.index((season, start))
    except ValueError:
        return []
    rest_of_season = [x for x in flat[i:] if x[0] == season]
    # Overshooting by a couple of episodes is usually a numbering mismatch (recap, ep 0,
    # double-length finale), not a subject spanning two seasons.
    if count is None or count - len(rest_of_season) <= 2:
        return rest_of_season[:count]
    return flat[i:i + count]


def describe(entry):
    if not entry.get("trakt_id") and not entry.get("trakt_slug"):
        return "-"
    s = entry.get("trakt_title") or entry.get("trakt_slug")
    if entry.get("type") == "show":
        s += f" S{entry.get('season', 1)}E{entry.get('episode_start', 1)}"
    return s


class Matcher:
    def __init__(self, trakt, tmdb=None):
        self.trakt = trakt
        self.tmdb = tmdb

    def _search(self, query, want):
        if self.tmdb:
            kinds = [("tv", "show"), ("movie", "movie")]
            if want == "movie":
                kinds.reverse()
            for tmdb_kind, kind in kinds:
                for res in self.tmdb.search(query, tmdb_kind)[:3]:
                    yield from self.trakt.lookup_tmdb(res["id"], kind)
        yield from self.trakt.search(query)

    def _candidates(self, subject, want):
        seen = set()
        for q in search_queries(subject):
            for r in self._search(q, want):
                kind = r["type"]
                if kind not in ("show", "movie"):
                    continue
                obj = r[kind]
                key = (kind, obj["ids"]["trakt"])
                if key not in seen:
                    seen.add(key)
                    yield kind, obj

    def _check(self, kind, obj, sdate):
        """Returns (entry, confident) or (None, False) if the candidate is implausible."""
        entry = {
            "type": kind,
            "trakt_id": obj["ids"]["trakt"],
            "trakt_slug": obj["ids"].get("slug"),
            "trakt_title": f"{obj.get('title')} ({obj.get('year')})",
        }
        year = obj.get("year")
        if kind == "movie":
            if not sdate or not year:
                return entry, False
            if year == sdate.year:
                return entry, True
            return (entry, False) if abs(year - sdate.year) <= 1 else (None, False)

        entry.update(season=1, episode_start=1)
        if sdate and year and year > sdate.year:
            return None, False  # show started after this subject aired
        if not sdate:
            return entry, False
        hit = locate_episode(self.trakt.seasons(entry["trakt_id"]), sdate)
        if hit:
            entry["season"], entry["episode_start"] = hit
            return entry, True
        return entry, False

    def match(self, subject):
        sdate = parse_date(subject.get("date"))
        want = wanted_kind(subject)
        fallback = None
        try:
            for i, (kind, obj) in enumerate(self._candidates(subject, want)):
                if i >= MAX_CANDIDATES:
                    break
                entry, confident = self._check(kind, obj, sdate)
                if entry is None:
                    continue
                if confident and (want is None or kind == want):
                    return {**entry, "status": "auto"}
                if fallback is None:
                    fallback = entry
        except ApiError as e:
            print(f"    ! 请求失败：{e}")
        if fallback:
            return {**fallback, "status": "uncertain"}
        return {"status": "not_found"}
