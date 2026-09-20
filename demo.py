"""Explicitly labelled local fixtures for offline interface and transfer checks."""
import base64
import hashlib
import subprocess
from pathlib import Path

from core import UserError

NAMES = ["海边的慢镜头", "晚风经过", "落日收集计划", "周末留白", "雨停之后", "向山而行", "城市的另一面", "月色漫游"]


class DemoAPI:
    def __init__(self, store, base_url):
        self.store, self.base_url = store, base_url
        self.folder = store.directory / "fixtures"
        self.folder.mkdir(exist_ok=True)
        for ext, codec in [("mp3", "libmp3lame"), ("flac", "flac")]:
            path = self.folder / ("sample." + ext)
            if not path.exists():
                self._make_sample(path, codec)
        self.user = dict(userId=10001, nickname="本地演示用户", avatarUrl="")
        self.store.user = self.user

    @staticmethod
    def _make_sample(path, codec):
        """演示模式需要一小段测试音频；ffmpeg 缺失时给出可读的提示。"""
        try:
            subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                            "sine=frequency=440:duration=5", "-c:a", codec, str(path)],
                           check=True, capture_output=True)
        except FileNotFoundError as e:
            raise UserError("演示模式需要 ffmpeg 生成测试音频，请先安装 ffmpeg 后重试。") from e
        except subprocess.CalledProcessError as e:
            detail = (e.stderr or b"").decode("utf-8", "replace").strip()[:200]
            raise UserError(f"生成演示音频失败：{detail or 'ffmpeg 执行出错'}") from e

    def available(self):
        return True

    def profile(self, cookie=None):
        return self.user

    def login(self, cookie):
        self.store.user = self.user
        return self.user

    def playlists(self):
        return [dict(id=9001 + i, name=name, coverImgUrl="", trackCount=8, creator={"nickname": "拾音演示"}, owned=i < 2)
                for i, name in enumerate(["把日子调成慢速", "路上的好天气", "深夜耳机时间"])]

    def playlist(self, playlist_id):
        playlists = {p["id"]: p for p in self.playlists()}
        if playlist_id not in playlists:
            raise UserError("演示模式请使用提供的三个歌单")
        return dict(playlist=playlists[playlist_id] | {"description": "本地演示歌单 · 下载的是 5 秒测试音频，用于验证队列、歌词和标签。"},
                    songs=[dict(id=80001 + i, name=name, artists="拾音测试音源", album="离线演示", duration=5000, cover="") for i, name in enumerate(NAMES)], missing=[], warnings=[])

    def songs(self, song_ids):
        """单曲下载走这条路径：演示模式只认 80001–80008 这几个测试音轨。"""
        catalog = {80001 + i: name for i, name in enumerate(NAMES)}
        found, missing = [], []
        for value in song_ids:
            text = str(value).strip()
            song_id = int(text) if text.isdigit() else -1
            if song_id in catalog:
                found.append(dict(id=song_id, name=catalog[song_id], artists="拾音测试音源",
                                  album="离线演示", duration=5000, cover=""))
            else:
                missing.append(value)
        if not found:
            raise UserError("演示模式没有这首歌，请使用 80001–80008 之间的 ID")
        return found, missing

    def resolve(self, song_id, quality, fallback=False):
        ext = "flac" if quality in {"lossless", "hires", "jymaster"} else "mp3"
        path = self.folder / ("sample." + ext)
        return dict(url=self.base_url + "/demo/audio/" + ext, ext=ext, actual_quality="lossless" if ext == "flac" else "standard", size=path.stat().st_size, md5=hashlib.md5(path.read_bytes()).hexdigest())

    def lyric(self, song_id):
        return {"lrc": {"lyric": "[00:00.000]This is a local test\n[00:02.000]Let the music slow down\n[00:04.000]Enjoy the quiet"},
                "tlyric": {"lyric": "[00:00.100]这是一段本地测试音频\n[00:02.050]让音乐慢下来\n[00:04.000]享受这一刻安静"},
                "romalrc": {"lyric": "[00:00.000]zhe shi yi duan ben di ce shi yin pin"}}

    def call(self, endpoint, params=None, **kwargs):
        if endpoint == "/login/qr/key":
            return {"data": {"unikey": "demo-only"}}
        if endpoint == "/login/qr/create":
            # qrcode is optional: demo mode must still work on the minimal install.
            try:
                import io
                import qrcode
                image = qrcode.make("本地演示二维码，不是真实登录凭据")
                output = io.BytesIO()
                image.save(output, format="PNG")
                return {"data": {"qrimg": "data:image/png;base64," + base64.b64encode(output.getvalue()).decode(), "qrurl": ""}}
            except ImportError:
                svg = ('<svg xmlns="http://www.w3.org/2000/svg" width="190" height="190">'
                       '<rect width="190" height="190" fill="#eef2e6"/>'
                       '<text x="95" y="90" font-size="13" text-anchor="middle" fill="#607b4d">演示模式</text>'
                       '<text x="95" y="112" font-size="10" text-anchor="middle" fill="#7b827a">未安装 qrcode</text>'
                       '</svg>')
                return {"data": {"qrimg": "data:image/svg+xml;base64," + base64.b64encode(svg.encode()).decode(), "qrurl": ""}}
        if endpoint == "/login/qr/check":
            return {"code": 801, "message": "演示模式无需扫码"}
        raise UserError("演示模式不会发送短信或连接真实账号")
