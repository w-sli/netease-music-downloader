# HTTP 接口参考

服务只监听 `127.0.0.1`，前端与接口同源。本文是前后端之间的约定，改接口时请同步更新本文与
`DEVELOPMENT.md` 的说明。任务记录与状态机的完整说明见 `DEVELOPMENT.md` 第 5 节。

## 通用约定

- 所有接口位于 `/api` 下，请求与响应均为 JSON（`/api/downloads/report` 例外，见下）。
- **错误**：HTTP 400/403/500 一律返回 `{error: "可读中文消息"}`。业务错误是 400，
  缺少/错误令牌是 403，未预期的异常是 500（日志只记异常类型与消息，不写堆栈，避免泄漏 Cookie）。
- **CSRF**：所有写操作（POST/PATCH/DELETE/PUT）必须带 `X-CSRF-Token` 请求头，
  值取自 `GET /api/bootstrap` 的 `csrf`。缺少或不匹配返回 403。
  服务重启会换令牌，前端收到 403 时会重新取一次令牌并重试一次。
- **额外防护**：`Host` 必须是回环地址；带 `Sec-Fetch-Site: cross-site` 的请求、以及 `Origin`
  与自身不同源的请求一律 403。写操作之外的 GET 同样受这两条保护。
- **接口来源**：`api_base` 只接受回环地址（`127.0.0.1` / `localhost` / `::1`，仅 http）。

## 启动与状态

| 接口 | 请求 | 响应 |
| --- | --- | --- |
| `GET /api/bootstrap` | — | `{csrf, settings, qualities:[{value,label}], user, api_available}` |
| `GET /api/session` | — | `{user}`，向接口重新确认登录状态 |
| `POST /api/cache/clear` | — | `{cleared:n}` 丢弃服务端缓存的歌单详情（前端“刷新歌单”会调用） |

`user` 未登录为 `null`，已登录为 `{userId,nickname,avatarUrl}`。

## 登录

| 接口 | 请求 | 响应 |
| --- | --- | --- |
| `POST /api/auth/qr` | — | `{key,image,url}`，`image` 是二维码 data URL |
| `POST /api/auth/qr/check` | `{key}` | `{code,message,user?}`：800 过期 / 801 待扫码 / 802 待确认 / 803 成功 |
| `POST /api/auth/sms/send` | `{phone,countrycode}` | `{ok:true}`，同一号码 60 秒内只允许发一次 |
| `POST /api/auth/sms/login` | `{phone,countrycode,captcha}` | `{user}` |
| `POST /api/auth/cookie` | `{cookie}` | `{user}`，Cookie 由用户自行从浏览器复制 |
| `POST /api/auth/logout` | — | `{ok:true}` |
| `POST /api/auth/from-splayer/undo` | — | `{user}`；把会话还原成读取 SPlayer 之前的样子（原来未登录就还原为未登录）。单次有效，之后其它登录动作会作废该还原点 |
| `POST /api/auth/from-splayer` | — | `{user}`；读取 SPlayer 配置目录里已有的登录态（只读、只取会话所需字段，读不到则返回错误） |

Cookie 只保存在数据目录的 `session.json`（权限 600），不下发到浏览器存储，
也不会出现在任何接口响应里。

## 歌单与单曲

| 接口 | 请求 | 响应 |
| --- | --- | --- |
| `GET /api/playlists` | — | `{playlists:[{id,name,coverImgUrl,trackCount,creator:{nickname},owned}],total}`，包含创建与收藏 |
| `GET /api/playlists/<id>` | — | `{playlist:{id,name,coverImgUrl,trackCount,description}, songs:[{id,name,artists,album,duration,cover}], missing:[id], warnings:[str]}` |

`songs[].duration` 单位为毫秒，`artists` 已拼接为字符串。`missing` 是无法获取详情的歌曲
（下架或无权限），`warnings` 是给用户看的说明。详情在服务端缓存 10 分钟。

歌单 ID 与单曲 ID 都由前端从「分享链接或纯数字」中解析，后端只接受数字 ID。

## 设置

| 接口 | 请求 | 响应 |
| --- | --- | --- |
| `GET /api/settings` | — | settings |
| `PATCH /api/settings` | 任意设置的子集 | 校验通过后的完整 settings |
| `POST /api/folder/pick` | — | `{path,cancelled}` 或错误；取消不是错误 |

settings 字段：`api_base`、`download_dir`、`quality`、`workers`(1–12)、`retries`(0–5)、
`translation`、`romanization`、`save_lrc`、`embed_lyrics`、`cover`、`playlist_folder`、
`playback_fallback`、`splayer_login`。`quality` 取值：`standard`、`higher`、`exhigh`、`lossless`、`hires`、
`jyeffect`、`sky`、`dolby`、`jymaster`（默认 `exhigh`，默认 4 并发）。

`PATCH` 的校验规则集中在 `core.Store.validated()`：未知字段、类型错误、越界数值、
非绝对路径或不可写的目录、非回环的 `api_base` 都会被拒。启动时载入 `settings.json` 用的是
同一套规则，因此手改文件不会绕过限制。

## 下载队列

| 接口 | 请求 | 响应 |
| --- | --- | --- |
| `POST /api/downloads` | 歌单：`{playlist_id, song_ids?}`；单曲：`{song_ids:[id]}` | `{added,existing,total,missing?}` |
| `GET /api/downloads` | — | 见下 |
| `POST /api/downloads/action` | `{action}` | `{ok:true}` |
| `POST /api/downloads/<job_id>/action` | `{action}` | `{ok:true}` |
| `GET /api/downloads/report` | — | 附件下载 `shiyin-downloads.json` |

**入队**：`song_ids` 省略或为 `null` 表示整单；空数组报错。带 `playlist_id` 时必须先打开过该歌单
（服务端有详情缓存），且所选歌曲必须属于该歌单。**不带 `playlist_id` 时进入单曲模式**：
只认 `song_ids`（可多个），直接按 ID 取详情，文件落在下载目录根下、不建歌单项子目录，
重试也保持同一目录；响应额外带 `missing`（无法获取的数量）。队列总上限 5000 条，超出报错。

**队列状态**（`GET /api/downloads`）：

```
{jobs:[{id, song_id, name, artists, album, playlist, single,
        status, progress, downloaded, total, speed,
        quality, actual_quality, path, error, warnings, attempt}],
 summary:{total, queued, active, completed, failed, cancelled, skipped, paused},
 paused:bool, workers:int}
```

- `status`：`queued` / `resolving` / `downloading` / `tagging` / `completed` / `failed` /
  `skipped` / `cancelled`。`completed` 与 `skipped` 都表示文件已在磁盘上，`skipped` 会在
  `warnings` 里说明原因是本工具自己的记录还是外部同名文件。
- `progress` 为 0–100，`speed` 为字节/秒，`single` 为 `true` 表示来自单曲下载。
- 任务按加入顺序倒序返回（最新在前）。**内部字段**（`settings`、`song`、`target_key`）
  不会出现在响应里；导出报告同样不含 Cookie 与音频 URL。
- `summary.paused` 在暂停时等于等待中的任务数，前端判断暂停状态请用顶层的 `paused`。

**队列操作**：

| action | 作用 |
| --- | --- |
| `pause` / `resume` | 停止/恢复领取新任务；**不中断**正在传输的文件 |
| `cancel_all` | 中断所有正在下载的文件，并取消等待中的任务 |
| `retry_failed` | 把失败与已取消的任务重新排队，并按**当前**设置重算目录与音质 |
| `clear_finished` | 从队列里移除已结束的记录，不动磁盘文件 |

单任务操作为 `retry`（失败/已取消的任务）与 `cancel`（等待中或进行中）。

