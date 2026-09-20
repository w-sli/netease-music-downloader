# 拾音 · 本地歌单下载器 — 前后端约定

所有 JSON API 位于同源 `/api`。失败 HTTP 4xx/5xx 返回 `{error: "可读中文消息"}`。启动网页用 GET `/api/bootstrap` 返回 `{csrf, settings, qualities, user, demo, api_available}`，JS 保存 csrf 并在所有 POST/PATCH/DELETE 使用 `X-CSRF-Token` 请求头。用户未登录 user 为 null，已登录 `{userId,nickname,avatarUrl}`。页面不要加载外部字体/JS。

settings: `{api_base, download_dir, quality, workers, retries, translation, romanization, save_lrc, embed_lyrics, cover, playlist_folder, playback_fallback}`。workers 1–12; retries 0–5; quality 为 standard,higher,exhigh,lossless,hires,jyeffect,sky,dolby,jymaster。默认 exhigh，4 workers。qualities 为 `[{value,label}]`。

- GET `/api/session` → `{user}`（验证当前会话）
- POST `/api/auth/qr` → `{key,image,url}` image 为 data URL
- POST `/api/auth/qr/check` `{key}` → `{code,message,user?}` 800过期801等待802确认803成功
- POST `/api/auth/sms/send` `{phone,countrycode}` → `{ok:true}`
- POST `/api/auth/sms/login` `{phone,countrycode,captcha}` → `{user}`
- POST `/api/auth/cookie` `{cookie}` → `{user}` 用户自行复制Cookie；输入使用密码框，不留浏览器存储
- POST `/api/auth/logout` → `{ok:true}`
- GET `/api/playlists` → `{playlists:[{id,name,coverImgUrl,trackCount,creator:{nickname},owned}],total}` 所有个人歌单含收藏和喜欢
- GET `/api/playlists/<id>` → `{playlist:{id,name,coverImgUrl,trackCount,description},songs:[{id,name,artists,album,duration,cover}],missing:[ids],warnings:[str]}` duration 毫秒，artists 已拼接字符串。支持用户直接输入歌单数字ID或网易分享链接，由前端提取id。
- GET `/api/settings` → settings
- PATCH `/api/settings` 传入 settings 可修改字段 → settings（目录立即检查是否可写）
- POST `/api/folder/pick` → `{path}` 系统目录选择器，不可用返回清楚错误；仍可手输路径
- POST `/api/downloads` `{playlist_id, song_ids:[id]}` song_ids 省略或null代表整单，空数组报错。→ `{added,existing,total}` 服务端按最新保存设置排队；需要先打开该歌单，后端缓存song详情。**`playlist_id` 为空时进入单曲下载模式**：只认 `song_ids`（可多个），按 ID 直接取歌曲详情，文件落在下载目录根下（不建歌单项子目录），重试也保持同一目录；返回额外带 `missing`（无法获取的数量）。
- GET `/api/downloads` → `{jobs:[{id,song_id,name,artists,album,playlist,single,status,progress,downloaded,total,speed,quality,actual_quality,path,error,warnings,attempt}],summary:{total,queued,active,completed,failed,paused,cancelled,skipped},paused,workers}` status queued/resolving/downloading/tagging/completed/failed/paused/cancelled/skipped，progress 0–100，speed bytes/s；`single` 为 true 表示该任务来自单曲下载。
- POST `/api/downloads/action` `{action:"pause"|"resume"|"retry_failed"|"cancel_all"|"clear_finished"}` → `{ok:true}` pause停止领取新任务，不中断正在传输的文件；cancel_all会中断正在下载；界面需准确描述。
- POST `/api/downloads/<job_id>/action` `{action:"retry"|"cancel"}` → `{ok:true}`
- GET `/api/downloads/report` → 下载 JSON 报告，不含cookie/url

页面设计要求：中文精致易用，侧边导航“我的歌单/下载队列/下载设置”；顶端账号与接口状态，未登录时有明显二维码登录入口；歌单网格+歌单详情歌曲可选（全选/筛选/整单下载），公开歌单链接入口；队列有总体进度/状态筛选/单项进度/速度/失败原因/重试/取消；设置下载目录/音质/并发/重试/歌词合并翻译罗马音/内嵌歌词/保存LRC/封面/按歌单建文件夹/API地址/播放地址后备；没有真实数据时正确空状态。配色暖灰底+深墨文本+森林绿强调，响应式桌面和窄屏，原生HTML/CSS/JS，无打包。图标可内联SVG，不依赖联网。

`--demo` 提供隔离的模拟账号、歌单与本地生成的音频下载（页面必须明显标识演示），用于完整交互测试，绝不冒充真实网易账号或资源。
