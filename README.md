# bangumi2trakt

> **English:** Migrate your [Bangumi](https://bgm.tv) (bgm.tv) anime / TV watch history, ratings and wishlist to [Trakt](https://trakt.tv).
> Bangumi stores every season (or cour) as a separate subject; this tool maps each one to the right Trakt show + season + episode by matching air dates, then writes history, ratings and watchlist via the Trakt API.
> Pure Python standard library, no dependencies (Python ≥ 3.9). Usage: `auth` → `match` → `review` → `sync --dry-run` → `sync` (see below).

把 Bangumi（bgm.tv）的动画/三次元观看记录、评分、想看列表同步到 Trakt。纯 Python 标准库，无需安装依赖（Python ≥ 3.9）。

适合打算从 Bangumi 搬家到 Trakt 的人：一次性迁移，也可以多次运行做增量同步。

## 映射规则

| Bangumi | Trakt |
|---|---|
| 看过 | 观看历史：该季全部集数（剧场版标记为看过） |
| 在看 / 搁置 / 抛弃 | 观看历史：按 Bangumi 进度（`ep_status`）标记前 N 集 |
| 想看 | Watchlist |
| 评分 1–10 | 评分 1–10（默认打在对应的**季**上，可改成剧集整体或两者都打） |
| 观看时间 | Bangumi 收藏的最后更新时间（可改为 `released` = 播出时间） |

已经在 Trakt 上标记过的集不会重复添加，所以可以放心多次运行。

## 准备

0. 下载代码：`git clone https://github.com/yohaneowo/bangumi2trakt.git`（或在 GitHub 页面点 Code → Download ZIP），然后在项目目录里运行下面的命令。
1. **Trakt 应用**：在 <https://trakt.tv/oauth/applications/new> 创建应用，Redirect URI 填 `urn:ietf:wg:oauth:2.0:oob`，拿到 Client ID / Client Secret。
2. **Bangumi Token**（可选）：只有收藏设为私密时才需要。在 <https://next.bgm.tv/demo/access-token> 生成。填了 token 时 `username` 可以留空。
3. **TMDB API Key**（可选，推荐）：TMDB 对日文、中文标题的搜索明显比 Trakt 自带搜索准。在 <https://www.themoviedb.org/settings/api> 申请，v3 key 或 v4 read token 都可以。
4. `copy config.example.json config.json` 并填写。`subject_types`：2 = 动画，6 = 三次元。

如需代理：设置环境变量 `HTTPS_PROXY=http://127.0.0.1:7890`（Windows 系统代理也会自动读取）。

## 使用

```powershell
python -m bgm2trakt auth                  # 1. 登录 Trakt
python -m bgm2trakt match --limit 20      # 2. 先试匹配 20 条看看效果
python -m bgm2trakt match                 #    再匹配全部（可中断，重跑会接着做）
python -m bgm2trakt review                # 3. 查看需要人工检查的条目
python -m bgm2trakt sync --dry-run        # 4. 预览将写入的内容
python -m bgm2trakt sync                  # 5. 正式写入
```

## 匹配原理与手动修正

Bangumi 每一季（常常是每一个 cour）都是单独的条目，而 Trakt 按 TMDB 的结构是「一部剧 + 多季」。
程序先用日文名、中文名以及去掉「第二季 / Season 2」等后缀的标题搜索，再用 Bangumi 条目的**放送日期**去找 Trakt 上同一天（±3 天）播出的那一集，从而确定季号和起始集。分割放送、绝对集数编号（如海贼王）也能处理。

匹配结果保存在 `data/mapping.json`，`status` 含义：

- `auto`：放送日期对上了，基本可信
- `uncertain`：标题搜到了但日期没对上，默认**不同步**（加 `--include-uncertain` 才同步）
- `not_found`：没搜到
- `manual`：你手动改的，重跑 match 永远不会覆盖
- `skip`：不要同步这一条

手动修正示例（`trakt_slug` 可以直接从 Trakt 网址里复制，例如 `trakt.tv/shows/attack-on-titan`）：

```json
"89414": {
  "bgm": { "...": "..." },
  "status": "manual",
  "type": "show",
  "trakt_slug": "attack-on-titan",
  "season": 2,
  "episode_start": 1,
  "episode_count": 12
}
```

`episode_count` 可选，用来覆盖 Bangumi 的集数（比如 Bangumi 和 Trakt 的集数不一致时）。改完后运行 `match --rematch uncertain` 不会动 manual 条目，直接 `sync` 即可。

## 已知限制

- Bangumi 没有记录每集具体的观看时间，所以同一季的所有集都使用收藏的更新时间；之后改过评分或吐槽的话，这个时间也会跟着变。
- 在看/搁置/抛弃只知道看了几集，默认按「前 N 集」处理。
- 短评不同步（Trakt 评论是公开的，且要求至少 5 个词）。
- Trakt 免费账户对 watchlist 有数量上限，想看的条目很多时可能会超出，可以把 `sync.watchlist` 设为 `false`。

## 隐私

`config.json`（API 密钥）和 `data/`（Trakt 登录令牌、匹配结果）只保存在本地，已在 `.gitignore` 中排除，**不要上传或分享**。程序只会访问 Bangumi、Trakt、TMDB 的官方 API。

## 许可证

[MIT](LICENSE)
