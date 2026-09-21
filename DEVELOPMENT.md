# 开发说明（面向二次开发）

本文写给要改这个项目的人。用户使用说明在 [README.md](README.md)，HTTP 接口细节在
[API_CONTRACT.md](API_CONTRACT.md)。

## 1. 它是什么

一个本机跑的 Flask 服务 + 原生前端，把本机网易云 API 服务的歌单/单曲下载到磁盘，
用 mutagen 写标签、合并歌词。**没有构建步骤**：改完 Python 重启进程，改完前端刷新页面。

- 运行依赖：Python 3.10+、`flask`、`requests`、`mutagen`
- 开发/测试额外需要：`pytest`、系统 `ffmpeg`
- 界面与文档优先中文，代码注释与文档字符串用英文

## 2. 模块划分与依赖方向

```
app.py            HTTP 层：路由、CSRF/Host 校验、设置读写、把请求翻译成队列操作
 ├─ core.py       配置与会话（Store）、接口客户端（Netease）、文件名清洗（safe_name）
 ├─ downloader.py 下载队列（DownloadManager）：并发调度、进度、重试、取消、落盘
 │   └─ lyrics.py 歌词合并（merge_lyrics）与写标签（write_tags），完全离线
 └─ demo.py       离线替身（仅 --demo），实现与 Netease 相同的接口面
static/app.js     前端逻辑：轮询队列、渲染、调用 /api
templates/index.html 单页模板（图标为内联 SVG，无外部依赖）
```

依赖是单向的：**`app` → `downloader` → (`core`, `lyrics`)**。
`core` 与 `lyrics` 互不依赖，`downloader` 不导入 `app`。
`demo.py` 只依赖 `core` 的 `UserError`，并且只被 `app` 在 `--demo` 时导入。

`Netease` 与 `DemoAPI` 必须保持**相同的方法面**（`available/profile/login/playlists/playlist/songs/resolve/lyric/call`）——
队列只认方法名，换实现不需要改 `downloader.py`。

## 3. 一次下载的完整数据流

```
前端 POST /api/downloads  {playlist_id?, song_ids: [...]}
  → app.enqueue()                单曲模式（无 playlist_id）走 api.songs() 取详情
  → DownloadManager.enqueue()    持锁建 job，状态 queued，写 downloads.json
  → worker 线程领取              状态 resolving
      api.resolve(id, quality)   /song/download/url/v1，可回退 /song/url/v1
  → _transfer()                  状态 downloading
      写临时文件 .shiyin-<job id>.<ext>，每 0.15s 更新 downloaded/progress/speed
  → 取歌词 + merge_lyrics()      状态 tagging（进度置 98）
  → write_tags()                 mutagen 写入标题/歌手/专辑/歌词/封面
  → _commit()                    硬链接；不支持硬链接时退化为独占创建后拷贝
  → 状态 completed，落盘记录
```

取消的实现：`cancel_events[job_id]` 是一个 `threading.Event`，传输循环每写一块就调用
`_check(event)`，被置位就抛 `Cancelled`，由 `_run_job` 转成 `cancelled` 状态并清理临时文件。

并发模型：启动时固定创建 **12 个 worker 线程**（`__init__` 里 `range(12)`），
每个线程在条件变量上等待，「同时进行的任务数 < `settings["workers"]`」才领取新任务。
**因此并发上限受线程池大小限制**：要放开上限，必须同时改线程池大小、`core.py` 的校验范围和
设置页滑块的 `max`，否则界面会显示一个根本不会生效的数字。

## 4. 关键设计决定（改之前请先读）

| 决定 | 为什么 | 改错的后果 |
| --- | --- | --- |
| 接口参数走 **URL query string**，不用 JSON body | 该 API 服务忽略请求体，只按 URL（含参数）区分请求与缓存 | 不同歌曲全部命中同一缓存，**下载到同一个文件** |
| 接口请求 **不跟随重定向** | `Cookie` 是手工设置的请求头，requests 跨主机跳转时不会剥离它 | 账号 Cookie 被发往其它主机 |
| 落盘用**硬链接**，失败退化为 `open(target,"xb")` 独占创建 | 两种方式都在目标已存在时报错，因此**永不覆盖**已有文件 | 用 `os.replace`/`shutil.copy` 会静默覆盖用户文件 |
| 取消/重试用**所有权令牌** `owners[job_id]` | 旧 worker 可能在新 worker 已接管后才执行清理 | 误删新任务的取消事件 → 取消失效、并发计数错乱 |
| 路径比较一律过 `path_key()`（`normcase`） | NTFS/exFAT 大小写不敏感 | 同名不同大小写的歌曲被判为「外部文件」而跳过，**少下歌** |
| 歌词匹配用**最近优先**（枚举容差内候选对后排序占用） | 与上游顺序双指针不同，见 `THIRD_PARTY_NOTICES.md` | 换成顺序贪心会改变边界行为，需同步改 `tests/test_lyrics.py` |
| `safe_name()` 同时处理分隔符、控制字符、双向控制符、Windows 保留名（含 `NUL.mp3`） | 名字来自接口，可能含任意字符 | 路径穿越或 Windows 上文件「下载成功但消失」 |
| 单曲下载 `single=True`，目录不建歌单项子目录 | 单曲没有歌单可归 | 重试时若按歌单规则重算，文件会跑进 `单曲下载 [0]/` |
| `queue.Queue` 不用，改为 `jobs` 列表 + 条件变量 | 需要按 id 查找、修改、持久化与展示 | — |
| `settings.json` 载入时**也走同一套校验** | 手改文件不能绕过约束 | api_base 可能被改成远程地址 |
| 全局异常只记录类型与消息，不记堆栈 | 请求头/请求体可能含账号 Cookie | 日志泄漏凭据 |

### 4.1 从 SPlayer 读取登录态（`core.read_splayer_cookie`）

「从 SPlayer 读取登录状态」这个入口读的是 SPlayer 自己 Electron 配置目录里的 Chromium
cookie 库（`<profile>/Cookies`，SQLite）。实现要点：

- 只读，且先复制到临时文件再打开：SPlayer 运行时数据库是加锁的，直接开会失败。
- 只取 `_SESSION_COOKIE_NAMES` 白名单里的字段。SPlayer 的 cookie 库里还混着名字就叫
  `Path`/`Expires`/`Max-Age` 的条目（它当年解析自己 Set-Cookie 头留下的），这些绝不能转发。
- 没有 `MUSIC_U` 就当没有会话，返回空串；值以 `v1` 开头（Chromium 加密后的形态）也直接放弃——
  **读不出来就当作没登录，绝不猜**。拿到的串仍要过一遍接口校验，所以不会产生假会话。
- 平台差异：Linux 上实测这些值是**明文**（`encrypted_value` 为空）；Windows/macOS 上 Chromium 会用
  DPAPI/Keychain 加密，此时该功能会静默失效（前端刻意不提示失败，只有成功才提示）。
  要支持那两个平台需要引入解密依赖，目前没做。

前端只在成功时提示一次（`loginFromSplayer()` 的 catch 是空的），因为它不是一个需要用户处理的操作。

该能力受设置项 `splayer_login` 控制（默认开）：关掉后登录窗口里的入口会隐藏，接口本身也会拒绝
（前端隐藏只是界面，服务端才是约束）。读取成功时会把**读取之前**的会话记在内存里
（`import_undo`），前端提示上因此可以给一个「撤回」按钮；还原点是单次的，并且任何其它登录/登出动作
都会把它作废，避免把陈旧会话还原回来。还原点不持久化——重启服务后自然失效。

## 5. 数据格式

### 5.1 数据目录

| 平台 | 路径 |
| --- | --- |
| Linux/macOS | `$XDG_CONFIG_HOME/shiyin-downloader`（缺省 `~/.config/shiyin-downloader`） |
| Windows | `%APPDATA%\shiyin-downloader` |
| `--demo` | 系统缓存目录下的 `shiyin-demo`（Linux `~/.cache/`、Windows `%LOCALAPPDATA%`） |

内容：`settings.json`（设置）、`session.json`（账号 Cookie，POSIX 权限 600）、
`downloads.json`（队列记录）、`fixtures/`（仅演示模式的测试音频）。

### 5.2 `settings.json`

字段与 `core.DEFAULTS` 一一对应：`api_base`、`download_dir`、`quality`、`workers`、
`retries`、`translation`、`romanization`、`save_lrc`、`embed_lyrics`、`cover`、
`playlist_folder`、`playback_fallback`。
校验规则集中在 `core.Store.validated()`——**新增字段必须同时改 `DEFAULTS`、`validated()`、
设置页控件与 `static/app.js` 的 `collectSettings()/fillSettingsForm()`**，否则会被当作未知字段拒绝。

### 5.3 `downloads.json` 中的任务记录

| 字段 | 说明 |
| --- | --- |
| `id` | 任务 UUID（临时文件名也用它） |
| `song_id` / `name` / `artists` / `album` | 展示与写标签用 |
| `playlist` / `playlist_id` / `single` | 展示来源；`single=True` 表示不在歌单目录下 |
| `status` | 见下面的状态机 |
| `progress` / `downloaded` / `total` / `speed` | 进度、字节数与速度 |
| `quality` / `actual_quality` | 请求音质与接口实际返回的音质 |
| `path` / `error` / `warnings` / `attempt` | 目标路径、失败原因、警告、第几次尝试 |
| `settings` / `song` / `target_key` | **内部字段**，`snapshot()` 会剥掉，不出现在 `/api/downloads` |

状态机：

```
queued ──> resolving ──> downloading ──> tagging ──> completed
   │            │              │             │
   │            └──────────────┴─────────────┴──> failed      (重试次数耗尽)
   └──────────────────────────────────────────> cancelled     (用户取消)
                    落盘时发现同名文件 ────────> skipped
```

`skipped` 与 `completed` 都表示「文件已在磁盘上」；`skipped` 附带说明原因是本工具自己的记录
还是外部同名文件。进程重启时处于进行中状态的任务会被重置为 `queued` 并给出提示。

## 6. 常见改动怎么做

**加一个接口**：在 `app.py` 里加路由（写操作记得走 `protect_local_app` 的 CSRF 校验，
错误直接 `raise UserError("中文说明")` 就会变成 400）→ 更新 `API_CONTRACT.md` →
前端 `static/app.js` 用 `api(path, {method, body})` 调用 → 补测试。

**加一种音质**：`core.QUALITIES` 加一项 → `templates/index.html` 的 `<select>` 加同名的
`<option>`（**文案必须与 `QUALITIES` 一致**，否则设置页与队列卡片会显示两个名字）→
`static/app.js` 的 `qualityLabel()` fallback 表补一项（仅为容错）。

**加一个设置项**：见 5.2 的清单，四处都要改；布尔项要确认 `validated()` 的类型检查覆盖到。

**加一个前端视图**：`templates/index.html` 加 `<section id="view-xxx" class="view" hidden>` →
`app.js` 的 `switchView()` 映射表 + 侧栏按钮加 `data-view="xxx"` → CSS 按现有 `.panel` 风格写。

**换接口服务**：只改设置里的 `api_base`。注意两种路径形态：独立服务在根路径，
SPlayer 带 `/api/netease` 前缀；`core.Store.validated()` 只接受回环地址。

**支持新的音频格式**：`lyrics._open_audio()` 的嗅探分支 + `write_tags()` 里对应分支 +
`core.Netease.resolve()` 的扩展名白名单，然后在 `tests/test_lyrics.py` 的
`formats` 里加一行（用例会自动覆盖）。

**接另一个歌词源**：`core.Netease.lyric()` 返回 `/lyric/new` 的形状即可，
`merge_lyrics()` 只认其中的 `lrc` / `tlyric` / `romalrc` 三个字段。

## 7. 测试

```bash
python -m pytest tests/ -q      # 66 项，需要 ffmpeg
```

| 文件 | 覆盖 |
| --- | --- |
| `tests/test_lyrics.py` | 歌词合并（容差、多时间戳、offset、去重）与五种格式的标签读写 |
| `tests/test_downloader.py` | 命名、去重、已有文件、落盘与回退、取消/重试、单曲、完整性提示 |
| `tests/test_core.py` | 设置校验、文件名清洗、接口客户端（含拒绝重定向）、畸形响应 |

两条硬规矩：

1. **测试不得访问外网。** 音频样本用 `ffmpeg` 现场生成，下载测试用 `127.0.0.1` 上的
   `ThreadingHTTPServer` 夹具。加了联网测试 CI 就会不稳定，这类 PR 会被拒。
2. **改行为就要加测试。** `os.link`、`path_key` 之类的分支可以用 `unittest.mock.patch` 直接模拟
   （见 `test_downloader.py` 里的硬链接回退与大小写不敏感用例）。

## 8. 已知限制与非目标

- **单实例假设**：两个进程同时指向同一个数据目录会互相覆盖 `downloads.json`（没有加文件锁）。
- **只支持本机接口**：`api_base` 限定回环地址，这是刻意的，不为远程服务开口子。
- **队列上限 5000 条**（`downloader.MAX_JOBS`）；上万首的歌单在内存与响应体上会有明显峰值，
  `/api/downloads` 目前不分页。
- **没有搜索功能**，也没有转码、没有专辑/歌手页、不写入 ReplayGain 等音量信息。
- **不绕过任何会员限制**：只使用官方接口，拿不到就失败并说明原因。
- **Windows**：已提供 `run.ps1` / `run.bat`、`%APPDATA%` 数据目录、系统目录选择器与
  UTF-8 控制台兜底，但**尚未在真实 Windows 上实测**；遇到问题请附系统与 Python 版本反馈。
- 下载目录允许任意可写路径（这是产品行为，不是漏洞）。

## 9. 调试

- 界面问题先用 `python app.py --demo`：离线、数据可预测、可反复重来（删掉缓存目录里的
  `shiyin-demo` 即可复位）。
- 服务端只把异常类型与消息写进日志（不写堆栈，避免把 Cookie 写进日志）。需要更多信息时
  在本地临时加 `app.logger.exception(...)`，**不要提交**。
- 常见症状对照：

| 症状 | 先查什么 |
| --- | --- |
| 写入操作返回 403 | 页面与服务端令牌不一致（服务重启过）——前端会自动刷新重试一次；持续失败则看 `Sec-Fetch-Site`/Origin |
| 任务一直 queued | 队列是否被暂停、`settings["workers"]` 是否为 0、worker 线程是否异常退出 |
| 队列徽章不动 | 轮询是否失败（连续失败 3 次会标记为已断开），或 `/api/downloads` 是否 500 |
| 下载到同一个文件 | 接口参数是否变成了请求体（见第 4 节第一条） |
| 提示「只返回试听片段」 | 账号没有该音质权限或歌曲下架，属预期 |
| 文件名与预期不同 | `safe_name()` 清洗、45/35/65 截断、同名歌曲加 `[歌曲ID]` |
