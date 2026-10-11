import argparse
import json
import sys
from collections import Counter
from pathlib import Path

from .bangumi import Bangumi
from .matcher import Matcher, describe
from .sync import apply_plan, build_plan
from .tmdb import TMDB
from .trakt import Trakt

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
CONFIG = ROOT / "config.json"
MAPPING = DATA / "mapping.json"
SUBJECTS = DATA / "subjects_cache.json"

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
    return Trakt(cfg["trakt"]["client_id"], cfg["trakt"]["client_secret"], DATA / "trakt_token.json")


def fetch_collections(cfg):
    b = cfg["bangumi"]
    bgm = Bangumi(b.get("access_token", ""))
    username = b.get("username") or bgm.me()["username"]
    items = []
    for st in b.get("subject_types", [2, 6]):
        items += bgm.collections(username, st)
    print(f"Bangumi 用户 {username}：共 {len(items)} 条收藏")
    return bgm, items


def cmd_auth(cfg, args):
    make_trakt(cfg).device_login()
    print("Trakt 授权成功")


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
    for n, it in enumerate(todo, 1):
        sid = str(it["subject_id"])
        if sid not in subjects:
            subjects[sid] = bgm.subject(sid)
        subj = subjects[sid]
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
        print(f"[{n}/{len(todo)}] {entry['status']:<9} {subj.get('name_cn') or subj['name']} → {describe(entry)}")
        if n % 10 == 0:
            save_json(MAPPING, mapping)
            save_json(SUBJECTS, subjects)
    save_json(MAPPING, mapping)
    save_json(SUBJECTS, subjects)
    print_counts(mapping)
    print("请运行 `python -m bgm2trakt review` 检查 uncertain / not_found 的条目")


def print_counts(mapping):
    counts = Counter(e["status"] for e in mapping.values())
    print("匹配结果：" + "，".join(f"{k} {v}" for k, v in sorted(counts.items())))


def cmd_review(cfg, args):
    mapping = load_json(MAPPING, {})
    print_counts(mapping)
    statuses = ("uncertain", "not_found") if not args.all else ("auto", "uncertain", "not_found", "manual")
    for sid, e in mapping.items():
        if e["status"] in statuses:
            b = e["bgm"]
            print(f"{e['status']:<9} {sid:>7}  {b.get('name_cn') or b['name']} ({b.get('date')}, {b.get('platform')}, {b.get('eps')}集)")
            print(f"{'':17}{b['url']}  →  {describe(e)}")


def resolve_manual(trakt, mapping):
    """Manual entries may give only trakt_slug (copied from a trakt.tv URL); fill in the numeric id."""
    changed = False
    for e in mapping.values():
        if e["status"] == "manual" and not e.get("trakt_id") and e.get("trakt_slug"):
            obj = trakt.summary(e["type"], e["trakt_slug"])
            e["trakt_id"] = obj["ids"]["trakt"]
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
        print(line)
    if skipped:
        print("跳过：" + "，".join(f"{k} {v}" for k, v in skipped.items()))
    bodies = plan.bodies()
    save_json(DATA / "last_plan.json", bodies)

    if args.dry_run:
        print(f"\n[dry-run] 没有写入 Trakt。完整请求内容见 {DATA / 'last_plan.json'}")
        return
    apply_plan(trakt, bodies)


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    DATA.mkdir(exist_ok=True)

    p = argparse.ArgumentParser(prog="bgm2trakt", description="把 Bangumi 观看记录和评分同步到 Trakt")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("auth", help="登录 Trakt（设备码授权）")
    m = sub.add_parser("match", help="把 Bangumi 条目匹配到 Trakt，写入 data/mapping.json")
    m.add_argument("--rematch", choices=["uncertain", "all"], help="重新匹配已有条目（manual/skip 永不覆盖）")
    m.add_argument("--limit", type=int, help="只处理前 N 条（试跑用）")
    r = sub.add_parser("review", help="列出需要人工检查的匹配")
    r.add_argument("--all", action="store_true", help="列出全部条目")
    s = sub.add_parser("sync", help="写入 Trakt")
    s.add_argument("--dry-run", action="store_true", help="只预览，不写入")
    s.add_argument("--include-uncertain", action="store_true", help="同时同步 uncertain 的匹配")
    args = p.parse_args()

    cfg = load_config()
    {"auth": cmd_auth, "match": cmd_match, "review": cmd_review, "sync": cmd_sync}[args.cmd](cfg, args)


if __name__ == "__main__":
    main()
