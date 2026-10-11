"""Map a Bangumi subject (one season / cour) onto a Trakt movie or a show + starting episode.

Bangumi splits every season (often every cour) into its own subject, while Trakt follows
TVDB/TMDB and has one show with seasons. We search Trakt by title, then use the subject's
air date to find the Trakt episode that premiered that day, which gives both the season
and the starting episode (this also handles split cours and absolute numbering).
"""
import logging
import re
from datetime import date, datetime, timedelta, timezone

from .net import ApiError

log = logging.getLogger(__name__)

MOVIE_PLATFORMS = {"剧场版", "电影", "Movie", "movie"}
EITHER_PLATFORMS = {"OVA", "OAD", "WEB"}
EP_TOLERANCE_DAYS = 3
SEASON_TOLERANCE_DAYS = 45
ROLLOVER_GAP_DAYS = 180
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


def season_span(season):
    """(first, last) air date of a season, or None if Trakt has no dates for it."""
    dates = [aired_date(e["first_aired"]) for e in season.get("episodes") or [] if e.get("first_aired")]
    return (min(dates), max(dates)) if dates else None


def locate_season(seasons, target, eps, slack=0):
    """A regular season that premiered within SEASON_TOLERANCE_DAYS of `target` and has
    `eps` (± slack) episodes. Covers wrong air dates on Trakt, and an episode 0 that aired a
    week before the season but sits in Trakt's specials. Returns (season, days off) or None."""
    if not eps:
        return None
    best = None
    for s in seasons:
        span = season_span(s)
        if s["number"] == 0 or not span or abs(len(s.get("episodes") or []) - eps) > slack:
            continue
        delta = abs((span[0] - target).days)
        if delta <= SEASON_TOLERANCE_DAYS and (best is None or delta < best[1]):
            best = (s["number"], delta)
    return best


def episode_range(seasons, season, start, count):
    """`count` consecutive episodes starting at (season, start).

    Regular seasons roll over into the next one when the overflow is large and the next
    season continues soon after (a split-cour Bangumi subject spanning two Trakt seasons);
    specials (season 0) never do. count=None means "rest of this season".
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
    # Only roll over while each next season picks up within ROLLOVER_GAP_DAYS of the
    # previous one; otherwise the extra episodes are web/bonus ones Trakt files elsewhere.
    spans = {s["number"]: season_span(s) for s in ordered}
    out = flat[i:i + count]
    for n, (a, b) in enumerate(zip(out, out[1:]), 1):
        if a[0] != b[0]:
            prev, nxt = spans.get(a[0]), spans.get(b[0])
            if not prev or not nxt or (nxt[0] - prev[1]).days > ROLLOVER_GAP_DAYS:
                return out[:n]
    return out


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
            log.debug("搜索：%s", q)
            for r in self._search(q, want):
                kind = r["type"]
                if kind not in ("show", "movie"):
                    continue
                obj = r[kind]
                key = (kind, obj["ids"]["trakt"])
                if key not in seen:
                    seen.add(key)
                    yield kind, obj

    def _check(self, kind, obj, sdate, eps=None, tv=False):
        """Returns (entry, confident, reason); entry is None if the candidate is implausible."""
        entry = {
            "type": kind,
            "trakt_id": obj["ids"]["trakt"],
            "trakt_slug": obj["ids"].get("slug"),
            "trakt_title": f"{obj.get('title')} ({obj.get('year')})",
        }
        year = obj.get("year")
        if kind == "movie":
            if not sdate or not year:
                return entry, False, "缺少年份，无法核对"
            if year == sdate.year:
                return entry, True, "上映年份一致"
            if abs(year - sdate.year) <= 1:
                return entry, False, f"上映年份差一年（{year} vs {sdate.year}）"
            return None, False, f"上映年份不符（{year} vs {sdate.year}）"

        entry.update(season=1, episode_start=1)
        if sdate and year and year > sdate.year:
            return None, False, f"剧集 {year} 年才开播，晚于条目 {sdate}"
        if not sdate:
            return entry, False, "Bangumi 条目没有放送日期，无法核对"
        seasons = self.trakt.seasons(entry["trakt_id"])
        hit = locate_episode(seasons, sdate)
        if hit and hit[0] == 0 and tv:
            # A TV subject landing on a special is usually an episode 0 / pre-air special;
            # prefer the regular season that starts right after, if there is one.
            season = locate_season(seasons, sdate, eps, slack=1)
            if season:
                entry["season"] = season[0]
                return entry, True, f"S0E{hit[1]} 播出日期相符，但 S{season[0]} 相差 {season[1]} 天开播且集数相近，按正片季处理"
        if hit:
            entry["season"], entry["episode_start"] = hit
            return entry, True, f"S{hit[0]}E{hit[1]} 的播出日期与 {sdate} 相符"
        season = locate_season(seasons, sdate, eps)
        if season:
            entry["season"] = season[0]
            return entry, True, f"S{season[0]} 首播与 {sdate} 相差 {season[1]} 天，但集数一致（{eps}）"
        return entry, False, f"没有在 {sdate} ±{EP_TOLERANCE_DAYS} 天内播出的集"

    def match(self, subject):
        sdate = parse_date(subject.get("date"))
        want = wanted_kind(subject)
        log.debug("放送日期 %s，期望类型 %s", sdate, want or "剧集或电影")
        fallback = None
        try:
            for i, (kind, obj) in enumerate(self._candidates(subject, want)):
                if i >= MAX_CANDIDATES:
                    log.debug("已检查 %d 个候选，停止", MAX_CANDIDATES)
                    break
                entry, confident, reason = self._check(kind, obj, sdate, subject.get("eps"), want == "show")
                log.debug("  候选 %s %s (%s, trakt %s)：%s", kind, obj.get("title"), obj.get("year"), obj["ids"]["trakt"], reason)
                if entry is None:
                    continue
                if confident and (want is None or kind == want):
                    return {**entry, "status": "auto"}
                if fallback is None:
                    fallback = entry
        except ApiError as e:
            log.warning(f"    ! 请求失败：{e}")
        if fallback:
            return {**fallback, "status": "uncertain"}
        return {"status": "not_found"}
