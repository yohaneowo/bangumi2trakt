import argparse
import json
import logging
import logging.handlers
import platform
import sys
import webbrowser
from collections import Counter
from pathlib import Path

from .bangumi import Bangumi
from .matcher import Matcher, describe
from .report import build_report, render
from .sync import apply_plan, build_plan
from .tmdb import TMDB
from .trakt import Trakt

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
CONFIG = ROOT / "config.json"
MAPPING = DATA / "mapping.json"
SUBJECTS = DATA / "subjects_cache.json"
LOG_FILE = DATA / "bgm2trakt.log"
REPORT = DATA / "report.html"

log = logging.getLogger("bgm2trakt")

DEFAULT_SYNC = {
    "history": True,
    "history_collection_types": [2, 3, 4, 5],
    "ratings": True,
    "rating_target": "season",
    "watchlist": True,
    "watched_at": "collection",
}


def load_json(path, default):
    return json.loads(path.read_text("utf-8")) if path.exists() else default


def save_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")


def load_config():
    if not CONFIG.exists():
        raise SystemExit("找不到 config.json，请复制 config.example.json 并填写")
    cfg = load_json(CONFIG, {})
    cfg["sync"] = {**DEFAULT_SYNC, **cfg.get("sync", {})}
    return cfg


def make_trakt(cfg):
    t = cfg["trakt"]
    return Trakt(t["client_id"], t.get("client_secret", ""), DATA / "trakt_token.json", t.get("redirect_uri", ""))


def fetch_collections(cfg):
    b = cfg["bangumi"]
    bgm = Bangumi(b.get("access_token", ""))
    username = bgm.username = b.get("username") or bgm.me()["username"]
    items = []
    for st in b.get("subject_types", [2, 6]):
        items += bgm.collections(username, st)
    log.info(f"Bangumi 用户 {username}：共 {len(items)} 条收藏")
    return bgm, items


def cmd_auth(cfg, args):
    make_trakt(cfg).login()
    log.info("Trakt 授权成功")


def needs_match(entry, rematch):
    if entry is None:
        return True
    if rematch == "all":
        return entry["status"] not in ("manual", "skip")
    if rematch == "uncertain":
        return entry["status"] in ("uncertain", "not_found")
    return False


def cmd_match(cfg, args):
    bgm, items = fetch_collections(cfg)
    trakt = make_trakt(cfg)
    tmdb_key = cfg.get("tmdb", {}).get("api_key")
    matcher = Matcher(trakt, TMDB(tmdb_key) if tmdb_key else None)
    mapping = load_json(MAPPING, {})
    subjects = load_json(SUBJECTS, {})

    todo = [it for it in items if needs_match(mapping.get(str(it["subject_id"])), args.rematch)]
    if args.limit:
        todo = todo[:args.limit]
    try:
        for n, it in enumerate(todo, 1):
            sid = str(it["subject_id"])
            if sid not in subjects:
                subjects[sid] = bgm.subject(sid)
            subj = subjects[sid]
            log.debug("--- [%d/%d] bgm %s %s / %s (%s, %s, %s集)", n, len(todo), sid, subj["name"],
                      subj.get("name_cn"), subj.get("date"), subj.get("platform"), subj.get("eps"))
            entry = matcher.match(subj)
            mapping[sid] = {
                "bgm": {
                    "name": subj["name"],
                    "name_cn": subj.get("name_cn", ""),
                    "date": subj.get("date"),
                    "platform": subj.get("platform"),
                    "eps": subj.get("eps"),
                    "url": f"https://bgm.tv/subject/{sid}",
                },
                **entry,
            }
            log.info(f"[{n}/{len(todo)}] {entry['status']:<9} {subj.get('name_cn') or subj['name']} → {describe(entry)}")
            if n % 10 == 0:
                save_json(MAPPING, mapping)
                save_json(SUBJECTS, subjects)
    finally:
        save_json(MAPPING, mapping)
        save_json(SUBJECTS, subjects)
    print_counts(mapping)
    log.info("请运行 `python -m bgm2trakt review` 检查 uncertain / not_found 的条目")


def print_counts(mapping):
    counts = Counter(e["status"] for e in mapping.values())
    log.info("匹配结果：" + "，".join(f"{k} {v}" for k, v in sorted(counts.items())))


def cmd_review(cfg, args):
    mapping = load_json(MAPPING, {})
    print_counts(mapping)
    statuses = ("uncertain", "not_found") if not args.all else ("auto", "uncertain", "not_found", "manual")
    for sid, e in mapping.items():
        if e["status"] in statuses:
            b = e["bgm"]
            log.info(f"{e['status']:<9} {sid:>7}  {b.get('name_cn') or b['name']} ({b.get('date')}, {b.get('platform')}, {b.get('eps')}集)")
            log.info(f"{'':17}{b['url']}  →  {describe(e)}")


def resolve_manual(trakt, mapping):
    """Manual entries may give only trakt_slug (copied from a trakt.tv URL) or only trakt_id; fill in the rest."""
    changed = False
    for e in mapping.values():
        complete = e.get("trakt_id") and e.get("trakt_slug") and e.get("trakt_title")
        if e["status"] == "manual" and not complete and (e.get("trakt_id") or e.get("trakt_slug")):
            obj = trakt.summary(e["type"], e.get("trakt_id") or e["trakt_slug"])
            e["trakt_id"] = obj["ids"]["trakt"]
            e["trakt_slug"] = obj["ids"]["slug"]
            e["trakt_title"] = f"{obj['title']} ({obj.get('year')})"
            changed = True
        if e["status"] == "manual" and e.get("type") == "show":
            e.setdefault("season", 1)
            e.setdefault("episode_start", 1)
    return changed


def cmd_sync(cfg, args):
    _, items = fetch_collections(cfg)
    trakt = make_trakt(cfg)
    mapping = load_json(MAPPING, {})
    if resolve_manual(trakt, mapping):
        save_json(MAPPING, mapping)

    plan, skipped = build_plan(items, mapping, trakt, cfg["sync"], args.include_uncertain)
    for line in plan.lines:
        log.info(line)
    if skipped:
        log.info("跳过：" + "，".join(f"{k} {v}" for k, v in skipped.items()))
    bodies = plan.bodies()
    save_json(DATA / "last_plan.json", bodies)

    if args.dry_run:
        log.info(f"\n[dry-run] 没有写入 Trakt。完整请求内容见 {DATA / 'last_plan.json'}")
        return
    apply_plan(trakt, bodies)
    log.info("可以运行 `python -m bgm2trakt report` 生成对照网页，检查 Trakt 上的结果")


def cmd_report(cfg, args):
    bgm, items = fetch_collections(cfg)
    trakt = make_trakt(cfg)
    mapping = load_json(MAPPING, {})
    if resolve_manual(trakt, mapping):
        save_json(MAPPING, mapping)
    rows = build_report(items, mapping, trakt, cfg["sync"])
    REPORT.write_text(render(rows, bgm.username), "utf-8")
    counts = Counter(r["verdict"] for r in rows)
    log.info(f"一致 {counts['ok']}，部分缺失 {counts['partial']}，缺失 {counts['missing']}，未同步 {counts['skipped']}")
    log.info(f"报告已生成：{REPORT}")
    if not args.no_open:
        webbrowser.open(REPORT.as_uri())


def setup_logging(verbose):
    """Terminal shows the usual progress; the log file keeps every detail (requests, match decisions, errors)."""
    log.setLevel(logging.DEBUG)
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(logging.Formatter("%(message)s"))
    file = logging.handlers.RotatingFileHandler(LOG_FILE, maxBytes=5_000_000, backupCount=3, encoding="utf-8")
    file.setLevel(logging.DEBUG)
    file.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    log.addHandler(console)
    log.addHandler(file)


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    DATA.mkdir(exist_ok=True)

    p = argparse.ArgumentParser(prog="bgm2trakt", description="把 Bangumi 观看记录和评分同步到 Trakt")
    p.add_argument("-v", "--verbose", action="store_true", help=f"终端也显示详细日志（无论是否开启，都会写入 {LOG_FILE.relative_to(ROOT)}）")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("auth", help="登录 Trakt")
    m = sub.add_parser("match", help="把 Bangumi 条目匹配到 Trakt，写入 data/mapping.json")
    m.add_argument("--rematch", choices=["uncertain", "all"], help="重新匹配已有条目（manual/skip 永不覆盖）")
    m.add_argument("--limit", type=int, help="只处理前 N 条（试跑用）")
    r = sub.add_parser("review", help="列出需要人工检查的匹配")
    r.add_argument("--all", action="store_true", help="列出全部条目")
    s = sub.add_parser("sync", help="写入 Trakt")
    s.add_argument("--dry-run", action="store_true", help="只预览，不写入")
    s.add_argument("--include-uncertain", action="store_true", help="同时同步 uncertain 的匹配")
    rp = sub.add_parser("report", help="生成网页报告，对照 Bangumi 和 Trakt 上的实际数据")
    rp.add_argument("--no-open", action="store_true", help="只生成文件，不自动打开浏览器")
    args = p.parse_args()

    setup_logging(args.verbose)
    log.debug("===== %s | Python %s | %s", " ".join(sys.argv[1:]), platform.python_version(), platform.platform())
    try:
        cfg = load_config()
        {"auth": cmd_auth, "match": cmd_match, "review": cmd_review, "sync": cmd_sync, "report": cmd_report}[args.cmd](cfg, args)
    except SystemExit as e:
        if isinstance(e.code, str):
            log.error(e.code)
            raise SystemExit(1) from None
        raise
    except KeyboardInterrupt:
        log.warning({
            "match": "已中断（已完成的匹配结果都已保存，重跑会接着做）",
            "sync": "已中断。可以直接重新运行 sync：已写入 Trakt 的观看记录会被跳过，不会重复",
        }.get(args.cmd, "已中断"))
        raise SystemExit(130) from None
    except Exception:
        log.exception("出错了")
        log.error(f"详细信息见 {LOG_FILE}，提 issue 时请附上这个文件（里面不含密钥）")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
