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

### 1. 下载代码

需要 Python 3.9 或以上（<https://www.python.org/downloads/>），不用安装任何第三方库。

```powershell
git clone https://github.com/yohaneowo/bangumi2trakt.git
cd bangumi2trakt
```

不会用 git 的话，在 GitHub 页面点 **Code → Download ZIP**，解压后在那个文件夹里打开终端。

### 2. 创建配置文件

```powershell
copy config.example.json config.json     # macOS / Linux：cp config.example.json config.json
```

然后打开 `config.json`，按下面的步骤填写。⚠️ 请改 `config.json`，不要改 `config.example.json`。前者已被 `.gitignore` 排除，不会被提交。

### 3. 填写各项资料

#### Bangumi 用户名（`bangumi.username`）

登录 bgm.tv，点右上角头像进入个人主页，地址栏里是 `https://bgm.tv/user/xxxxx`，`xxxxx` 就是用户名（可能是英文，也可能是数字）。

#### Bangumi Access Token（`bangumi.access_token`，可选）

只有收藏设为私密时才需要。不确定的话先留空，如果运行时显示收藏数为 0 再回来填。

1. 打开 <https://next.bgm.tv/demo/access-token> 并登录
2. 点「创建个人令牌」，名称随便写，有效期选长一点
3. 复制生成的令牌填进去。填了 token 后 `username` 可以留空

#### Trakt Client ID / Secret（`trakt.client_id` / `trakt.client_secret`，必填）

1. 注册或登录 <https://trakt.tv>
2. 打开 <https://trakt.tv/oauth/applications/new>
3. 填写表单：
   - **Name**：随便，例如 `bangumi2trakt`
   - **Redirect uri**：`http://localhost:8765/callback`（必须一字不差，和 `config.json` 里的 `trakt.redirect_uri` 一致）
   - 其他栏位留空，复选框都不用勾
4. 点 **Save App**，在应用页面上复制 **Client ID** 填进 `client_id`
5. **Client Secret** 分两种情况：
   - 页面上有一串 Secret：复制填进 `client_secret`，登录时会用设备码方式（打开网址输入代码）
   - 显示 *Not issued. This app signs users in with PKCE*：`client_secret` 留空即可，登录时会自动打开浏览器授权，授权完跳回本机完成登录

> 如果 8765 端口被占用，可以换一个端口，但 Trakt 应用里的 Redirect uri 和 `config.json` 里的 `trakt.redirect_uri` 要一起改。

#### TMDB API Key（`tmdb.api_key`，可选但强烈推荐）

TMDB 对日文、中文标题的搜索明显比 Trakt 自带的搜索准，不填的话匹配成功率会低很多。

1. 注册或登录 <https://www.themoviedb.org>
2. 打开 <https://www.themoviedb.org/settings/api>，点「创建」，类型选 **Developer**
3. 同意条款并填写申请表：用途选个人用途，应用 URL 随便填（例如 `https://bgm.tv`），简介写「个人使用，同步观看记录」即可，一般会立即通过
4. 复制 **API 密钥**（较短的那串，v3）。下面较长的「API 读访问令牌」（v4）也可以用，二选一

填完大概是这样：

```json
{
  "bangumi": { "username": "xxxxx", "access_token": "", "subject_types": [2, 6] },
  "trakt":   { "client_id": "abc123...", "client_secret": "（没有就留空）", "redirect_uri": "http://localhost:8765/callback" },
  "tmdb":    { "api_key": "789xyz..." },
  "sync":    { "...": "一般不用改，见下表" }
}
```

### 4. 同步选项（`sync`，可选）

| 选项 | 默认值 | 说明 |
|---|---|---|
| `history` | `true` | 是否同步观看历史 |
| `history_collection_types` | `[2, 3, 4, 5]` | 哪些状态写入观看历史：2 看过、3 在看、4 搁置、5 抛弃 |
| `ratings` | `true` | 是否同步评分 |
| `rating_target` | `"season"` | 剧集评分打在哪：`season` 对应季、`show` 整部剧、`both` 两者都打（同一部剧多季评分取平均） |
| `watchlist` | `true` | 是否把「想看」加入 Trakt watchlist（免费账户有数量上限） |
| `watched_at` | `"collection"` | 观看时间：`collection` = Bangumi 收藏的更新时间，`released` = 播出时间 |

`bangumi.subject_types`：2 = 动画，6 = 三次元，默认两者都同步。

### 代理

如需代理，设置环境变量 `HTTPS_PROXY`，例如 PowerShell 里 `$env:HTTPS_PROXY="http://127.0.0.1:7890"`。Windows 系统代理也会自动读取。

## 使用

在项目目录里依次运行：

```powershell
python -m bgm2trakt auth                  # 1. 登录 Trakt
python -m bgm2trakt match --limit 20      # 2. 先试匹配 20 条看看效果
python -m bgm2trakt match                 #    再匹配全部
python -m bgm2trakt review                # 3. 查看需要人工检查的条目
python -m bgm2trakt sync --dry-run        # 4. 预览将写入的内容（不会写入）
python -m bgm2trakt sync                  # 5. 正式写入
```

**1. `auth` 登录 Trakt**
按提示在浏览器里授权即可，只需要做一次。登录信息保存在 `data/trakt_token.json`，过期前会自动续期。

**2. `match` 匹配**
把每个 Bangumi 条目对应到 Trakt 上的剧集（第几季、从第几集开始）或电影，结果保存在 `data/mapping.json`。每条大约 3 秒，几百条要十几分钟。随时可以按 Ctrl+C 中断，重跑会从中断处继续，已匹配的不会重做。

**3. `review` 检查**
列出 `uncertain`（搜到了但日期对不上）和 `not_found`（没搜到）的条目，这些默认**不会同步**。每条都附有 Bangumi 链接和当前的匹配结果，按下面的[手动修正](#手动修正)改好即可。想看为什么没匹配上，可以在 `data/bgm2trakt.log` 里搜这个条目的名字。加 `--all` 会列出全部条目。

**4. `sync --dry-run` 预览**
列出每个条目将要写入的内容，例如：

```
[看过] 进击的巨人 最终季 Part.2 → Attack on Titan (2013): S4E17–S4E28 (12集), 评分 9
[看过] 路人女主的养成方法 Fine → Saekano the Movie: Finale (2019): 标记看过, 评分 10
[想看] 葬送的芙莉莲 → Frieren: Beyond Journey's End (2023): 加入 watchlist
```

建议大致过一遍，重点看季号和集数对不对。完整的请求内容保存在 `data/last_plan.json`。

**5. `sync` 写入**
先生成同步计划（每部剧需要读取一次分集信息，约 1 秒一部，终端会显示进度），然后写入 Trakt，最后显示各项新增了多少。

- 已经在 Trakt 上标记过的集会自动跳过，重复运行不会产生重复记录，所以中断后直接重跑即可
- 写入的观看记录只能在 Trakt 网页上手动删除，所以第 4 步的预览一定要看

## 匹配原理

Bangumi 每一季（常常是每一个 cour）都是单独的条目，而 Trakt 按 TMDB 的结构是「一部剧 + 多季」。程序按以下步骤匹配：

1. **搜索**：用日文名、中文名，以及去掉「第二季 / Season 2 / Part.2」等后缀的标题，在 TMDB 和 Trakt 上搜索
2. **按播出日期定位**：用 Bangumi 条目的放送日期，找 Trakt 上同一天（±3 天）播出的那一集，从而确定季号和起始集。分割放送、绝对集数编号（如海贼王）也能处理
3. **Trakt 日期有误时**：如果没有哪一集的日期对得上，但某一季的首播在 45 天以内、集数也一致，同样视为匹配成功
4. **第 0 话 / 先行特别篇**：TV 动画如果日期撞上的是特别篇（S0），而紧接着有一个集数相近的正片季开播，就按正片季处理（例如正片开播前一周先播的第 0 话）
5. **跨季**：Bangumi 条目的集数比 Trakt 那一季多出 3 集以上，且下一季在半年内接着播出时，多出来的集会接到下一季（例如 Bangumi 把 2 个 cour 算作一个条目）。否则多出的集视为网络版或特典，不往后接

电影按上映年份核对，差一年会标为 `uncertain`。

`data/mapping.json` 中 `status` 的含义：

| status | 含义 | 会同步吗 |
|---|---|---|
| `auto` | 日期对上了，基本可信 | ✅ |
| `uncertain` | 标题搜到了，但日期没对上 | ❌（加 `--include-uncertain` 才同步） |
| `not_found` | 没搜到 | ❌ |
| `manual` | 你手动修正的，重跑 `match` 永远不会覆盖 | ✅ |
| `skip` | 不要同步这一条 | ❌ |

## 手动修正

用编辑器打开 `data/mapping.json`，用 `review` 输出里的条目编号（例如 `27832`）找到对应的条目，把 `status` 改成 `"manual"`，再按下面的格式填写 Trakt 信息。`bgm` 那一段保留原样即可。

**剧集：**

```json
"27832": {
  "bgm": { "...": "保留原样" },
  "status": "manual",
  "type": "show",
  "trakt_slug": "mobile-suit-gundam-seed",
  "season": 1,
  "episode_start": 1,
  "episode_count": 50
}
```

**电影：**

```json
"425211": {
  "bgm": { "...": "保留原样" },
  "status": "manual",
  "type": "movie",
  "trakt_slug": "kaguya-sama-love-is-war-the-first-kiss-that-never-ends-2022"
}
```

**特别篇 / OVA**（Trakt 上通常放在第 0 季，也就是 Specials）：

```json
"2811": {
  "bgm": { "...": "保留原样" },
  "status": "manual",
  "type": "show",
  "trakt_slug": "kanokon",
  "season": 0,
  "episode_start": 13,
  "episode_count": 2
}
```

各字段的说明：

| 字段 | 说明 |
|---|---|
| `type` | `"show"`（剧集）或 `"movie"`（电影） |
| `trakt_slug` | Trakt 网址里的那一段，例如 `trakt.tv/shows/attack-on-titan` 里的 `attack-on-titan`。也可以改用数字 `trakt_id` |
| `season` | 第几季，特别篇是 `0`。可在 `trakt.tv/shows/<slug>/seasons/<季号>` 查看 |
| `episode_start` | 从第几集开始，默认 `1` |
| `episode_count` | 可选。共几集，默认使用 Bangumi 的集数。Bangumi 和 Trakt 集数不一致时填写 |

改完直接运行 `sync --dry-run` 确认即可，不用重新 `match`。不想同步的条目，把 `status` 改成 `"skip"`。

## 已知限制

- Bangumi 没有记录每集具体的观看时间，所以同一季的所有集都使用收藏的更新时间；之后改过评分或吐槽的话，这个时间也会跟着变。
- 在看/搁置/抛弃只知道看了几集，默认按「前 N 集」处理。
- 短评不同步（Trakt 评论是公开的，且要求至少 5 个词）。
- Trakt 免费账户对 watchlist 有数量上限，想看的条目很多时可能会超出，可以把 `sync.watchlist` 设为 `false`。

## 日志与排错

每次运行的详细过程都会写入 `data/bgm2trakt.log`，终端只显示进度。日志里包括：

- 每个 Bangumi 条目搜了哪些标题、看了哪些 Trakt 候选，以及每个候选被接受或排除的原因（例如「没有在 2019-04-06 ±3 天内播出的集」）
- 每个 API 请求的地址、状态码和耗时，以及重试和网络错误
- 同步时 Trakt 的完整返回，包括 Trakt 找不到的具体条目
- 出错时的完整错误堆栈

加 `-v` 可以让终端也显示这些细节，例如 `python -m bgm2trakt -v match --limit 5`。日志文件超过 5 MB 会自动轮换。

日志不含 API 密钥和登录令牌，提 issue 时可以直接附上。

## 隐私

`config.json`（API 密钥）和 `data/`（Trakt 登录令牌、匹配结果）只保存在本地，已在 `.gitignore` 中排除，**不要上传或分享**。程序只会访问 Bangumi、Trakt、TMDB 的官方 API。

## 许可证

[MIT](LICENSE)
