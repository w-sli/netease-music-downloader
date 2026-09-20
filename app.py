#!/usr/bin/env python3
"""拾音: local SPlayer-compatible playlist downloader."""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
import webbrowser
from pathlib import Path
from urllib.parse import urlparse

from flask import Flask, Response, jsonify, render_template, request, send_file
from werkzeug.exceptions import HTTPException
from werkzeug.serving import make_server

from core import Netease, QUALITIES, Store, UserError
from downloader import DownloadManager


def create_app(data_dir=None, demo=False, port=36523, api=None):
    app = Flask(__name__)
    app.config.update(MAX_CONTENT_LENGTH=1024 * 1024, JSON_AS_ASCII=False)
    store = Store(data_dir or Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "shiyin-downloader")
    if demo:
        from demo import DemoAPI
        store.settings["download_dir"] = str(store.directory / "demo-downloads")
        api = DemoAPI(store, f"http://127.0.0.1:{port}")
    api = api or Netease(store)
    manager = DownloadManager(store, api)
    csrf = secrets.token_urlsafe(32)
    playlist_cache = {}
    playlist_lock = threading.Lock()
    qr_keys = {}
    sms_times = {}
    app.extensions.update(store=store, music_api=api, downloads=manager)

    @app.before_request
    def protect_local_app():
        if request.host.split(":")[0] not in {"127.0.0.1", "localhost"}:
            return jsonify(error="仅支持本机访问"), 403
        origin = request.headers.get("Origin")
        if origin and origin != request.host_url.rstrip("/"):
            return jsonify(error="不允许跨站访问"), 403
        if request.method in {"POST", "PATCH", "DELETE", "PUT"}:
            if not secrets.compare_digest(request.headers.get("X-CSRF-Token", ""), csrf):
                return jsonify(error="页面会话已更新，请刷新页面后重试"), 403

    @app.after_request
    def response_headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'self'; img-src 'self' data: https: http:; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        if request.path.startswith("/api"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.errorhandler(UserError)
    def user_error(error):
        return jsonify(error=str(error)), 400

    @app.errorhandler(Exception)
    def unexpected(error):
        if isinstance(error, HTTPException):
            return jsonify(error=error.description), error.code
        app.logger.error("Request failed: %s", type(error).__name__)
        return jsonify(error="操作未完成，请检查输入或重试。"), 500

    def body():
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            raise UserError("请求必须为 JSON 对象")
        return data

    def get_playlist(pid):
        with playlist_lock:
            entry = playlist_cache.get(pid)
        if entry and time.monotonic() - entry[0] < 600:
            return entry[1]
        data = api.playlist(pid)
        with playlist_lock:
            if len(playlist_cache) >= 30:
                playlist_cache.pop(next(iter(playlist_cache)))
            playlist_cache[pid] = (time.monotonic(), data)
        return data

    def invalidate_playlists():
        with playlist_lock:
            playlist_cache.clear()

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/api/bootstrap")
    def bootstrap():
        return jsonify(csrf=csrf, settings=store.settings,
                       qualities=[{"value": value, "label": label} for value, label in QUALITIES],
                       user=store.user, demo=demo, api_available=api.available())

    @app.get("/api/session")
    def session_status():
        store.user = api.profile()
        return jsonify(user=store.user)

    @app.post("/api/cache/clear")
    def cache_clear():
        """Drop cached playlist details so the next read comes from the API."""
        with playlist_lock:
            cleared = len(playlist_cache)
            playlist_cache.clear()
        return jsonify(cleared=cleared)

    @app.post("/api/auth/qr")
    def qr_create():
        data = api.call("/login/qr/key", cookie="")
        key = data.get("data", {}).get("unikey")
        if not key:
            raise UserError("接口未返回二维码 key")
        image = api.call("/login/qr/create", {"key": key, "qrimg": True}, cookie="").get("data", {})
        if not image.get("qrimg"):
            raise UserError("二维码生成失败，请刷新重试")
        now = time.monotonic()
        for stale in [k for k, t in qr_keys.items() if now - t > 300]:
            qr_keys.pop(stale, None)
        qr_keys[key] = now
        return jsonify(key=key, image=image["qrimg"], url=image.get("qrurl", ""))

    @app.post("/api/auth/qr/check")
    def qr_check():
        key = body().get("key")
        if key not in qr_keys or time.monotonic() - qr_keys[key] > 300:
            return jsonify(code=800, message="二维码已过期，请刷新")
        result = api.call("/login/qr/check", {"key": key}, cookie="", allow_codes=(800, 801, 802, 803))
        code = result.get("code")
        messages = {800: "二维码已过期", 801: "使用网易云音乐 App 扫码", 802: "已扫码，请在手机上确认", 803: "登录成功"}
        response = dict(code=code, message=messages.get(code, "登录状态异常"))
        if code == 803:
            response["user"] = api.login(result.get("cookie", ""))
            qr_keys.pop(key, None)
            invalidate_playlists()
        return jsonify(response)

    def phone_fields(data):
        phone = str(data.get("phone", "")).strip()
        country = str(data.get("countrycode", "86")).strip().lstrip("+")
        if not re.fullmatch(r"\d{5,16}", phone) or not re.fullmatch(r"\d{1,4}", country):
            raise UserError("请输入有效手机号和国家区号")
        return phone, country

    @app.post("/api/auth/sms/send")
    def sms_send():
        phone, country = phone_fields(body())
        key = (phone, country)
        if time.monotonic() - sms_times.get(key, -1000) < 60:
            raise UserError("验证码发送后请等待 60 秒")
        api.call("/captcha/sent", {"phone": phone, "ctcode": country}, cookie="")
        sms_times[key] = time.monotonic()
        return jsonify(ok=True)

    @app.post("/api/auth/sms/login")
    def sms_login():
        data = body()
        phone, country = phone_fields(data)
        captcha = str(data.get("captcha", "")).strip()
        if not re.fullmatch(r"\d{4,8}", captcha):
            raise UserError("请输入有效验证码")
        result = api.call("/login/cellphone", {"phone": phone, "countrycode": country, "captcha": captcha}, cookie="")
        user = api.login(result.get("cookie", ""))
        invalidate_playlists()
        return jsonify(user=user)

    @app.post("/api/auth/cookie")
    def cookie_login():
        user = api.login(body().get("cookie", ""))
        invalidate_playlists()
        return jsonify(user=user)

    @app.post("/api/auth/logout")
    def logout():
        store.save_session("", None)
        invalidate_playlists()
        return jsonify(ok=True)

    @app.get("/api/playlists")
    def playlists():
        result = api.playlists()
        return jsonify(playlists=result, total=len(result))

    @app.get("/api/playlists/<int:playlist_id>")
    def playlist(playlist_id):
        return jsonify(get_playlist(playlist_id))

    @app.get("/api/settings")
    def get_settings():
        return jsonify(store.settings)

    @app.patch("/api/settings")
    def settings():
        old_base = store.settings["api_base"]
        result = store.update(body())
        if old_base != result["api_base"]:
            invalidate_playlists()
            store.user = None
        manager.notify_settings()
        return jsonify(result)

    @app.post("/api/folder/pick")
    def folder_pick():
        if shutil.which("zenity"):
            command = ["zenity", "--file-selection", "--directory", "--title=选择音乐下载目录"]
        elif shutil.which("kdialog"):
            command = ["kdialog", "--getexistingdirectory", str(Path.home())]
        else:
            raise UserError("未安装系统目录选择器，请直接填写目录的绝对路径")
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=300)
        except subprocess.TimeoutExpired as e:
            raise UserError("目录选择超时，请直接填写路径") from e
        if result.returncode or not result.stdout.strip():
            # Dismissing the chooser is a normal action, not a failure.
            return jsonify(cancelled=True, path="")
        return jsonify(path=result.stdout.strip(), cancelled=False)

    @app.post("/api/downloads")
    def enqueue():
        data = body()
        try:
            pid = int(data.get("playlist_id"))
        except (TypeError, ValueError) as e:
            raise UserError("请先选择歌单") from e
        playlist = get_playlist(pid)
        songs = playlist["songs"]
        if data.get("song_ids") is not None:
            ids = data["song_ids"]
            if not isinstance(ids, list) or not ids or any(type(i) is not int for i in ids):
                raise UserError("请选择要下载的歌曲")
            available = {s["id"] for s in songs}
            if set(ids) - available:
                raise UserError("部分歌曲不在当前歌单中，请刷新歌单")
            songs = [s for s in songs if s["id"] in set(ids)]
        if not songs:
            raise UserError("歌单没有可下载的歌曲")
        return jsonify(manager.enqueue(playlist["playlist"], songs))

    @app.get("/api/downloads")
    def downloads():
        return jsonify(manager.snapshot())

    @app.post("/api/downloads/action")
    def downloads_action():
        action = body().get("action")
        if action not in {"pause", "resume", "retry_failed", "cancel_all", "clear_finished"}:
            raise UserError("不支持的队列操作")
        return jsonify(manager.action(action))

    @app.post("/api/downloads/<job_id>/action")
    def job_action(job_id):
        action = body().get("action")
        if action not in {"retry", "cancel"}:
            raise UserError("不支持的任务操作")
        return jsonify(manager.action(action, job_id))

    @app.get("/api/downloads/report")
    def report():
        payload = json.dumps(manager.snapshot(), ensure_ascii=False, indent=2).encode("utf-8")
        return send_file(io.BytesIO(payload), mimetype="application/json", as_attachment=True, download_name="shiyin-downloads.json")

    if demo:
        @app.get("/demo/audio/<ext>")
        def demo_audio(ext):
            if ext not in {"mp3", "flac"}:
                return "", 404
            path = api.folder / ("sample." + ext)
            def chunks():
                with path.open("rb") as f:
                    while chunk := f.read(2048):
                        time.sleep(.1)
                        yield chunk
            return Response(chunks(), content_type="audio/mpeg" if ext == "mp3" else "audio/flac", headers={"Content-Length": str(path.stat().st_size)})

    return app


def main():
    parser = argparse.ArgumentParser(description="拾音 · 本地多线程歌单下载器")
    parser.add_argument("--port", type=int, default=36523)
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--demo", action="store_true", help="使用独立的离线演示账号和测试音频")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("port 必须在 1024–65535 之间")
    data_dir = args.data_dir
    if args.demo and data_dir is None:
        data_dir = Path.home() / ".cache" / "shiyin-demo"
    app = create_app(data_dir, args.demo, args.port)
    try:
        server = make_server("127.0.0.1", args.port, app, threaded=True)
    except SystemExit:
        app.extensions["downloads"].close()
        raise
    address = f"http://127.0.0.1:{args.port}"
    print(f"拾音已启动：{address}" + ("（离线演示）" if args.demo else ""), flush=True)
    if not args.no_browser:
        threading.Timer(.5, lambda: webbrowser.open(address)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.extensions["downloads"].close()
        server.server_close()


if __name__ == "__main__":
    main()
