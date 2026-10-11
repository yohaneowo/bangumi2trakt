"""Compare Bangumi collections with what is actually on Trakt now, as a local HTML page."""
import json
import logging
from datetime import datetime

from .bangumi import COLLECTION_TYPES
from .sync import target_episodes

log = logging.getLogger(__name__)

SYNCED = ("auto", "manual")
SKIP_REASONS = {
    "uncertain": "匹配不确定，未同步",
    "not_found": "Trakt 上没找到，未同步",
    "skip": "已设为 skip，未同步",
    "unmatched": "还没有匹配，请先运行 match",
}


def fmt_eps(eps):
    if not eps:
        return ""
    (s1, e1), (s2, e2) = eps[0], eps[-1]
    if len(eps) == 1:
        return f"S{s1}E{e1}"
    return f"S{s1}E{e1}–E{e2}" if s1 == s2 else f"S{s1}E{e1}–S{s2}E{e2}"


def trakt_url(e, season=None):
    slug = e.get("trakt_slug") or e.get("trakt_id")
    if e.get("type") == "movie":
        return f"https://trakt.tv/movies/{slug}"
    url = f"https://trakt.tv/shows/{slug}"
    return f"{url}/seasons/{season}" if season is not None else url


def fetch_trakt_state(trakt):
    log.info("正在读取 Trakt 上的观看记录、评分和 watchlist……")
    state = {
        "movies": {m["movie"]["ids"]["trakt"] for m in trakt.watched("movies")},
        "episodes": trakt.watched_episodes(),
        "movie_ratings": {},
        "season_ratings": {},
        "show_ratings": {},
        "watchlist": set(),
    }
    for r in trakt.get_all("/sync/ratings", auth=True):
        if r["type"] == "movie":
            state["movie_ratings"][r["movie"]["ids"]["trakt"]] = r["rating"]
        elif r["type"] == "season":
            state["season_ratings"][(r["show"]["ids"]["trakt"], r["season"]["number"])] = r["rating"]
        elif r["type"] == "show":
            state["show_ratings"][r["show"]["ids"]["trakt"]] = r["rating"]
    for w in trakt.get_all("/sync/watchlist", auth=True):
        if w["type"] in ("movie", "show"):
            state["watchlist"].add((w["type"], w[w["type"]]["ids"]["trakt"]))
    return state


def check_item(it, e, trakt, state, opts):
    """One report row: what Bangumi says, where it went on Trakt, and whether Trakt has it."""
    subj, ctype = it["subject"], it["type"]
    row = {
        "id": it["subject_id"],
        "name": subj.get("name_cn") or subj["name"],
        "name_jp": subj["name"] if subj.get("name_cn") else "",
        "bgm_url": f"https://bgm.tv/subject/{it['subject_id']}",
        "image": (subj.get("images") or {}).get("common") or "",
        "date": subj.get("date") or "",
        "ctype": COLLECTION_TYPES[ctype],
        "bgm_rate": it.get("rate") or 0,
        "ep_status": it.get("ep_status") or 0,
        "eps": subj.get("eps") or 0,
    }
    status = e["status"] if e else "unmatched"
    row["status"] = status
    if e and e.get("trakt_id"):
        row["trakt_title"] = e.get("trakt_title") or e.get("trakt_slug") or str(e["trakt_id"])
        row["kind"] = e["type"]
        row["trakt_url"] = trakt_url(e, e.get("season") if e["type"] == "show" else None)
    if status not in SYNCED:
        row["verdict"], row["detail"] = "skipped", SKIP_REASONS[status]
        return row

    tid, kind = e["trakt_id"], e["type"]
    problems, notes = [], []

    if ctype == 1:
        if opts["watchlist"]:
            if (kind, tid) in state["watchlist"]:
                notes.append("已在 watchlist")
            else:
                problems.append("不在 watchlist")
    elif opts["history"] and ctype in opts["history_collection_types"]:
        if kind == "movie":
            if ctype == 2:
                if tid in state["movies"]:
                    notes.append("已标记看过")
                else:
                    problems.append("Trakt 上没有观看记录")
        else:
            eps = target_episodes(it, e, trakt)
            if eps is None:
                notes.append("Bangumi 进度为 0 集")
            elif not eps:
                problems.append("找不到对应的集，请检查 mapping")
            else:
                have = state["episodes"].get(tid, set())
                missing = [x for x in eps if x not in have]
                row["target"] = fmt_eps(eps)
                row["have"], row["want"] = len(eps) - len(missing), len(eps)
                row["trakt_url"] = trakt_url(e, eps[0][0])
                if missing:
                    shown = "、".join(f"S{s}E{n}" for s, n in missing[:8])
                    problems.append(f"缺 {len(missing)} 集：{shown}" + ("……" if len(missing) > 8 else ""))
                else:
                    notes.append(f"{len(eps)} 集全部已看")

    if opts["ratings"] and it.get("rate"):
        if kind == "movie":
            rating = state["movie_ratings"].get(tid)
        elif opts["rating_target"] == "show":
            rating = state["show_ratings"].get(tid)
        else:
            rating = state["season_ratings"].get((tid, e.get("season", 1)))
        row["trakt_rate"] = rating or 0
        if not rating:
            problems.append("Trakt 上没有评分")

    if problems:
        partial = row.get("have") and not any("评分" in p or "watchlist" in p for p in problems)
        row["verdict"] = "partial" if partial else "missing"
        row["detail"] = "；".join(problems)
    else:
        row["verdict"], row["detail"] = "ok", "；".join(notes)
    return row


def build_report(items, mapping, trakt, opts):
    state = fetch_trakt_state(trakt)
    log.info("正在逐条核对（每部剧需要读取一次分集信息，约 1 秒一部）……")
    rows = []
    for n, it in enumerate(items, 1):
        if n % 25 == 0 or n == len(items):
            log.info("  %d/%d", n, len(items))
        rows.append(check_item(it, mapping.get(str(it["subject_id"])), trakt, state, opts))
    return rows


def render(rows, username):
    data = {
        "rows": rows,
        "user": username,
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    return TEMPLATE.replace("/*DATA*/null", payload)


TEMPLATE = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Bangumi → Trakt 同步报告</title>
<style>
:root {
  --bg: #f6f7f9; --surface: #ffffff; --text: #1d2026; --muted: #6b7280; --line: #e5e7eb;
  --accent: #ed1c24; --chip: #eef0f3;
  --ok: #15803d; --ok-bg: #dcfce7; --partial: #b45309; --partial-bg: #fef3c7;
  --missing: #b91c1c; --missing-bg: #fee2e2; --skipped: #4b5563; --skipped-bg: #e5e7eb;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #111317; --surface: #1a1d23; --text: #e8eaed; --muted: #9aa0a8; --line: #2b2f37;
    --accent: #ff4d55; --chip: #252932;
    --ok: #4ade80; --ok-bg: #14301f; --partial: #fbbf24; --partial-bg: #3a2c0c;
    --missing: #f87171; --missing-bg: #3b1515; --skipped: #b0b6bf; --skipped-bg: #2b2f37;
  }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--text);
  font: 14px/1.5 system-ui, -apple-system, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif; }
.wrap { max-width: 1100px; margin: 0 auto; padding: 24px 16px 64px; }
h1 { font-size: 22px; margin: 0 0 4px; }
.sub { color: var(--muted); margin-bottom: 20px; }
.stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(130px, 1fr)); gap: 10px; margin-bottom: 20px; }
.stat { background: var(--surface); border: 1px solid var(--line); border-radius: 10px; padding: 12px 14px;
  cursor: pointer; text-align: left; color: inherit; font: inherit; }
.stat.active { outline: 2px solid var(--accent); outline-offset: -2px; }
.stat b { display: block; font-size: 24px; line-height: 1.2; font-variant-numeric: tabular-nums; }
.stat span { color: var(--muted); font-size: 13px; }
.stat.ok b { color: var(--ok); } .stat.partial b { color: var(--partial); }
.stat.missing b { color: var(--missing); } .stat.skipped b { color: var(--skipped); }
.bar { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin-bottom: 14px; }
.bar input { flex: 1 1 220px; min-width: 0; padding: 8px 12px; border-radius: 8px; border: 1px solid var(--line);
  background: var(--surface); color: var(--text); font: inherit; }
.chip { border: 0; border-radius: 999px; padding: 6px 12px; background: var(--chip); color: var(--text);
  cursor: pointer; font: inherit; font-size: 13px; }
.chip.active { background: var(--accent); color: #fff; }
.count { color: var(--muted); font-size: 13px; margin-left: auto; }
.list { display: flex; flex-direction: column; gap: 8px; }
.row { display: grid; grid-template-columns: 56px minmax(0, 1fr) minmax(0, 1fr) 150px; gap: 14px; align-items: center;
  background: var(--surface); border: 1px solid var(--line); border-radius: 10px; padding: 10px 14px; }
.poster { width: 56px; height: 78px; border-radius: 6px; object-fit: cover; background: var(--chip); display: block; }
.title { font-weight: 600; overflow-wrap: anywhere; }
.title a { color: inherit; text-decoration: none; } .title a:hover { text-decoration: underline; }
.jp { color: var(--muted); font-size: 12px; overflow-wrap: anywhere; }
.meta { color: var(--muted); font-size: 12px; margin-top: 4px; display: flex; flex-wrap: wrap; gap: 4px 10px; }
.side-label { color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: .04em; }
.target { font-variant-numeric: tabular-nums; }
.badge { display: inline-block; border-radius: 6px; padding: 2px 8px; font-size: 12px; font-weight: 600; }
.badge.ok { color: var(--ok); background: var(--ok-bg); } .badge.partial { color: var(--partial); background: var(--partial-bg); }
.badge.missing { color: var(--missing); background: var(--missing-bg); } .badge.skipped { color: var(--skipped); background: var(--skipped-bg); }
.detail { color: var(--muted); font-size: 12px; margin-top: 4px; overflow-wrap: anywhere; }
.progress { height: 4px; border-radius: 2px; background: var(--chip); margin-top: 6px; overflow: hidden; }
.progress i { display: block; height: 100%; background: var(--ok); }
.empty { text-align: center; color: var(--muted); padding: 40px 0; }
@media (max-width: 720px) {
  .row { grid-template-columns: 48px minmax(0, 1fr); }
  .poster { width: 48px; height: 66px; grid-row: span 3; align-self: start; }
  .status { grid-column: 2; }
}
</style>
</head>
<body>
<div class="wrap">
  <h1>Bangumi → Trakt 同步报告</h1>
  <div class="sub" id="sub"></div>
  <div class="stats" id="stats"></div>
  <div class="bar">
    <input id="q" type="search" placeholder="搜索中文名、日文名或 Trakt 标题">
  </div>
  <div class="bar" id="chips"></div>
  <div class="list" id="list"></div>
</div>
<script>
const DATA = /*DATA*/null;
const LABEL = { ok: "一致", partial: "部分缺失", missing: "缺失", skipped: "未同步" };
const STATUS = { auto: "自动匹配", manual: "手动修正", uncertain: "不确定", not_found: "没找到", skip: "跳过", unmatched: "未匹配" };
const state = { verdict: "problem", ctype: "全部", kind: "全部", q: "" };
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

document.getElementById("sub").textContent =
  `Bangumi 用户 ${DATA.user} · 共 ${DATA.rows.length} 条收藏 · 生成于 ${DATA.generated} · 数据读取自 Trakt 当前状态`;

function stats() {
  const n = v => DATA.rows.filter(r => r.verdict === v).length;
  const cards = [
    ["problem", n("partial") + n("missing"), "需要检查", "missing"],
    ["ok", n("ok"), "一致", "ok"],
    ["partial", n("partial"), "部分缺失", "partial"],
    ["missing", n("missing"), "缺失", "missing"],
    ["skipped", n("skipped"), "未同步", "skipped"],
    ["all", DATA.rows.length, "全部", ""],
  ];
  document.getElementById("stats").innerHTML = cards.map(([k, v, label, cls]) =>
    `<button class="stat ${cls} ${state.verdict === k ? "active" : ""}" data-v="${k}"><b>${v}</b><span>${label}</span></button>`).join("");
}

function chips() {
  const group = (key, values) => values.map(v =>
    `<button class="chip ${state[key] === v ? "active" : ""}" data-k="${key}" data-v="${v}">${v}</button>`).join("");
  document.getElementById("chips").innerHTML =
    group("ctype", ["全部", "看过", "在看", "想看", "搁置", "抛弃"]) +
    `<span style="width:8px"></span>` +
    group("kind", ["全部", "剧集", "电影"]).replace(/data-k="kind" data-v="全部">全部/, 'data-k="kind" data-v="全部">全部类型') +
    `<span class="count" id="count"></span>`;
}

function matches(r) {
  if (state.verdict === "problem" && !(r.verdict === "partial" || r.verdict === "missing")) return false;
  if (!["problem", "all"].includes(state.verdict) && r.verdict !== state.verdict) return false;
  if (state.ctype !== "全部" && r.ctype !== state.ctype) return false;
  if (state.kind === "剧集" && r.kind !== "show") return false;
  if (state.kind === "电影" && r.kind !== "movie") return false;
  const q = state.q.trim().toLowerCase();
  return !q || [r.name, r.name_jp, r.trakt_title].some(s => (s || "").toLowerCase().includes(q));
}

function row(r) {
  const meta = [r.ctype, r.date, r.eps ? `${r.eps} 集` : "", r.bgm_rate ? `评分 ${r.bgm_rate}` : "",
    r.ctype !== "看过" && r.ctype !== "想看" && r.ep_status ? `看到第 ${r.ep_status} 集` : ""].filter(Boolean);
  const trakt = r.trakt_title
    ? `<div class="title"><a href="${esc(r.trakt_url)}" target="_blank" rel="noopener">${esc(r.trakt_title)}</a></div>
       <div class="meta">${[r.kind === "movie" ? "电影" : "剧集", r.target ? `<span class="target">${esc(r.target)}</span>` : "",
         r.trakt_rate ? `评分 ${r.trakt_rate}` : "", STATUS[r.status]].filter(Boolean).join(" · ")}</div>`
    : `<div class="detail">—</div>`;
  const pct = r.want ? Math.round(100 * r.have / r.want) : null;
  return `<div class="row">
    ${r.image ? `<img class="poster" loading="lazy" src="${esc(r.image)}" alt="">` : `<div class="poster"></div>`}
    <div><div class="side-label">Bangumi</div>
      <div class="title"><a href="${esc(r.bgm_url)}" target="_blank" rel="noopener">${esc(r.name)}</a></div>
      ${r.name_jp ? `<div class="jp">${esc(r.name_jp)}</div>` : ""}
      <div class="meta">${meta.map(esc).join(" · ")}</div></div>
    <div><div class="side-label">Trakt</div>${trakt}</div>
    <div class="status"><span class="badge ${r.verdict}">${LABEL[r.verdict]}${r.want ? ` ${r.have}/${r.want}` : ""}</span>
      ${pct !== null ? `<div class="progress"><i style="width:${pct}%"></i></div>` : ""}
      ${r.detail ? `<div class="detail">${esc(r.detail)}</div>` : ""}</div>
  </div>`;
}

function render() {
  stats(); chips();
  const rows = DATA.rows.filter(matches);
  document.getElementById("count").textContent = `显示 ${rows.length} 条`;
  document.getElementById("list").innerHTML = rows.length ? rows.map(row).join("")
    : `<div class="empty">${state.verdict === "problem" ? "没有需要检查的条目 🎉 Trakt 上的数据和 Bangumi 一致" : "没有符合条件的条目"}</div>`;
}

document.addEventListener("click", e => {
  const s = e.target.closest(".stat"); if (s) { state.verdict = s.dataset.v; render(); return; }
  const c = e.target.closest(".chip"); if (c) { state[c.dataset.k] = c.dataset.v; render(); }
});
document.getElementById("q").addEventListener("input", e => { state.q = e.target.value; render(); });
if (!DATA.rows.some(r => r.verdict === "partial" || r.verdict === "missing")) state.verdict = "all";
render();
</script>
</body>
</html>
"""
