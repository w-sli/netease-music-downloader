# 贡献指南

感谢有兴趣参与。这个项目很小，几条约定能让改动更容易被接受。

## 开发环境

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
python -m pytest tests/ -q          # 需要系统安装 ffmpeg
python app.py --demo                # 离线演示，用来手动验证界面
```

测试**不允许访问外网**：音频样本用 ffmpeg 现场生成，下载行为用 `127.0.0.1` 上的本地
HTTP 夹具模拟。任何需要联网才能通过的测试都会被拒收——那样 CI 会不稳定。

前端是原生 HTML/CSS/JS，没有打包步骤：改完 `templates/` 或 `static/` 直接刷新页面即可。

## 提交改动前

1. `python -m pytest tests/ -q` 全部通过。
2. 手动跑一次 `python app.py --demo`，确认受影响的界面还能用（尤其是下载队列）。
3. 如果改了前后端接口，同步更新 `API_CONTRACT.md`。
4. 不要提交 `session.json`、`settings.json`、`downloads.json`、任何音频/歌词文件或 `.venv`
   （`.gitignore` 已覆盖，但请自己确认 `git status` 干净）。

## 请务必不要做的事

- 不要提交你的账号 Cookie、手机号、uid 或任何真实歌单数据；Issue 里也不要贴。
- 不要添加绕过版权保护的功能（第三方音源解锁、破解会员音质等），本项目只使用官方接口。
- 不要引入需要联网的测试，也不要为了通过测试而放宽生产代码的安全校验（CSRF、回环限制、
  路径清洗、不覆盖已有文件、不跟随重定向）。

## 提交与 PR

- 提交信息用中文或英文都可以，首行说明改了什么。
- PR 描述里请写：动机、改动范围、验证方式（贴 `pytest` 结果或复现步骤）。
- 涉及行为的改动（命名规则、落盘方式、并发语义）请附上测试或说明为什么不可测。

## 报告问题

- 缺陷请用 Issue 模板，附操作系统、Python 版本、接口服务来源（Docker / npx / SPlayer）、
  复现步骤与报错文本（**不要附 Cookie 或账号信息**）。
- 安全问题请走 `SECURITY.md` 里写的渠道，不要开公开 Issue。
