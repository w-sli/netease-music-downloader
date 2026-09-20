"""Session storage and the same Netease API contract used by SPlayer."""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

import requests

QUALITIES = [
    ("standard", "标准 · 128 kbps"), ("higher", "较高 · 192 kbps"),
    ("exhigh", "极高 · 320 kbps"), ("lossless", "无损 · FLAC"),
    ("hires", "Hi-Res"), ("jyeffect", "高清臻音"), ("sky", "沉浸环绕"),
    ("dolby", "杜比全景声"), ("jymaster", "超清母带"),
]
DEFAULTS = dict(api_base="http://127.0.0.1:25884/api/netease",
                download_dir=str(Path.home() / "Music" / "拾音"),
                quality="exhigh", workers=4, retries=2, translation=True,
                romanization=False, save_lrc=True, embed_lyrics=True, cover=True,
                playlist_folder=True, playback_fallback=False)
# Hard ceiling so a malformed or hostile API response cannot drive an endless loop.
MAX_SONGS = 20000


class UserError(Exception):
    pass


def atomic_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(value, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, path)
        path.chmod(0o600)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


class Store:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock = threading.RLock()
        self.settings = self._load_settings()
        self.cookie = read_json(self.directory / "session.json", {}).get("cookie", "")
        self.user = None

    def _load_settings(self):
        """settings.json is untrusted input: a hand-edited or older file must not
        bypass the constraints enforced on save."""
        raw = read_json(self.directory / "settings.json", {})
        if not isinstance(raw, dict):
            return dict(DEFAULTS)
        candidate = {key: value for key, value in (DEFAULTS | raw).items() if key in DEFAULTS}
        try:
            return self.validated(candidate, probe=False)
        except UserError:
            return dict(DEFAULTS)

    def validated(self, values, probe=True):
        """Return a normalized copy of `values`, raising UserError when invalid."""
        if not isinstance(values, dict) or set(values) - set(DEFAULTS):
            raise UserError("设置包含未知字段")
        new = dict(values)
        if not isinstance(new["api_base"], str):
            raise UserError("API 地址必须是文本")
        base = urlparse(new["api_base"])
        # Account cookies stay on loopback; an API server can be forwarded via SSH.
        if base.scheme != "http" or base.hostname not in {"127.0.0.1", "localhost", "::1"} or base.username or base.password or base.query or base.fragment:
            raise UserError("API 地址必须是本机 http://127.0.0.1:端口，可附加 /api/netease")
        new["api_base"] = new["api_base"].rstrip("/")
        if new["quality"] not in dict(QUALITIES):
            raise UserError("不支持的音质")
        for key, lo, hi in [("workers", 1, 12), ("retries", 0, 5)]:
            if type(new[key]) is not int or not lo <= new[key] <= hi:
                raise UserError(f"{key} 必须是 {lo}–{hi} 之间的整数")
        for key, val in DEFAULTS.items():
            if isinstance(val, bool) and type(new[key]) is not bool:
                raise UserError(f"{key} 必须是布尔值")
        if not isinstance(new["download_dir"], str) or not new["download_dir"].strip():
            raise UserError("请填写下载目录")
        folder = Path(new["download_dir"]).expanduser()
        if not folder.is_absolute():
            raise UserError("下载目录必须是绝对路径")
        if probe:
            try:
                folder.mkdir(parents=True, exist_ok=True)
                with tempfile.TemporaryFile(dir=folder):
                    pass
            except OSError as e:
                raise UserError(f"下载目录不可写：{e.strerror}") from e
        new["download_dir"] = str(folder.resolve())
        return new

    def save_session(self, cookie, user):
        with self.lock:
            atomic_json(self.directory / "session.json", {"cookie": cookie})
            self.cookie, self.user = cookie, user

    def update(self, values):
        if not isinstance(values, dict) or set(values) - set(DEFAULTS):
            raise UserError("设置包含未知字段")
        new = self.validated(self.settings | values)
        with self.lock:
            atomic_json(self.directory / "settings.json", new)
            self.settings = dict(new)
        return dict(new)


class Netease:
    def __init__(self, store):
        self.store = store

    def call(self, endpoint, params=None, cookie=None, allow_codes=()):
        headers = {"Cookie": self.store.cookie if cookie is None else cookie}
        try:
            # A local API endpoint must not be sent to an environment HTTP proxy.
            with requests.Session() as session:
                session.trust_env = False
                # Parameters must travel in the query string: the Netease API
                # service ignores JSON bodies and would otherwise answer every
                # request from one shared cache entry (same URL, wrong song).
                # Redirects stay off: the account cookie is an explicit header,
                # and requests does not strip it when a redirect leaves the host.
                response = session.post(self.store.settings["api_base"] + endpoint,
                                        params=dict(params or {}, timestamp=int(time.time() * 1000)),
                                        headers=headers, timeout=(5, 35), allow_redirects=False)
                if response.is_redirect:
                    raise UserError("音乐接口返回了重定向，已中止，以免把账号凭据转发到其它主机。请检查 API 地址是否正确。")
                data = response.json()
            if not isinstance(data, dict):
                raise UserError("音乐接口返回了无法识别的数据")
            code = data.get("code", 200)
            if code not in (200, *allow_codes) or response.status_code >= 400:
                if code in (301, 302):
                    raise UserError("登录已失效，请重新登录")
                message = data.get("message") or data.get("msg") or "请求失败"
                raise UserError(f"音乐接口：{str(message)[:200]}（{code}）")
            return data
        except UserError:
            raise
        except ValueError as e:
            # requests' JSONDecodeError subclasses both RequestException and
            # ValueError, so this must come first to report the real problem.
            raise UserError("API 地址返回了非 JSON 内容，请检查是否包含 /api/netease") from e
        except requests.RequestException as e:
            raise UserError("无法连接音乐接口。请打开 SPlayer，或启动独立 API 服务后检查设置。") from e

    def available(self):
        try:
            with requests.Session() as s:
                s.trust_env = False
                r = s.get(self.store.settings["api_base"] + "/login/status",
                          timeout=(1, 3), allow_redirects=False)
                if not r.ok:
                    return False
                body = r.json()
                return isinstance(body, dict) and isinstance(body.get("data"), dict)
        except (requests.RequestException, ValueError):
            return False

    def profile(self, cookie=None):
        data = self.call("/login/status", cookie=cookie).get("data", {})
        profile = data.get("profile")
        if not profile:
            return None
        return {k: profile.get(k) for k in ("userId", "nickname", "avatarUrl")}

    def login(self, cookie):
        if not isinstance(cookie, str) or not cookie.strip() or len(cookie) > 32768 or "\n" in cookie or "\r" in cookie:
            raise UserError("Cookie 格式不正确")
        user = self.profile(cookie.strip())
        if not user:
            raise UserError("Cookie 已失效或登录未完成")
        self.store.save_session(cookie.strip(), user)
        return user

    def playlists(self):
        user = self.profile()
        self.store.user = user
        if not user:
            raise UserError("请先登录，再查看个人歌单")
        result, seen = [], set()
        for offset in range(0, 100000, 100):
            data = self.call("/user/playlist", {"uid": user["userId"], "limit": 100, "offset": offset})
            page = data.get("playlist", [])
            if not isinstance(page, list):
                raise UserError("个人歌单接口返回了无法识别的数据")
            added = 0
            for item in page:
                song_id = item.get("id") if isinstance(item, dict) else None
                if song_id is None or song_id in seen:
                    continue
                seen.add(song_id)
                creator = item.get("creator") if isinstance(item.get("creator"), dict) else {}
                result.append({k: item.get(k) for k in ("id", "name", "coverImgUrl", "trackCount", "creator")}
                              | {"owned": item.get("userId", creator.get("userId")) == user["userId"]})
                added += 1
            if not page or data.get("more") is False or (len(page) < 100 and not data.get("more")):
                return result
            if not added:
                raise UserError("个人歌单接口重复返回相同一页，请重试")
        raise UserError("个人歌单超过分页安全上限")

    def playlist(self, playlist_id):
        raw = self.call("/playlist/detail", {"id": playlist_id, "s": 0}).get("playlist")
        if not isinstance(raw, dict) or not raw:
            raise UserError("歌单不存在或当前账号无权访问")
        track_ids = raw.get("trackIds") if isinstance(raw.get("trackIds"), list) else []
        ids = []
        for entry in track_ids:
            if isinstance(entry, dict) and str(entry.get("id", "")).isdigit():
                ids.append(int(entry["id"]))
            if len(ids) >= MAX_SONGS:
                break
        songs = {}
        if ids:
            # Chunk ids so the query string stays well under common URL limits.
            for offset in range(0, len(ids), 100):
                page = self.call("/song/detail", {"ids": ",".join(map(str, ids[offset:offset + 100]))})
                for song in page.get("songs") or []:
                    if isinstance(song, dict) and str(song.get("id", "")).isdigit():
                        songs[int(song["id"])] = normalize_song(song)
        else:
            try:
                count = min(int(raw.get("trackCount", 0)), MAX_SONGS)
            except (TypeError, ValueError):
                count = 0
            for offset in range(0, count, 500):
                page = self.call("/playlist/track/all", {"id": playlist_id, "limit": 500, "offset": offset})
                for song in page.get("songs") or []:
                    if isinstance(song, dict) and str(song.get("id", "")).isdigit():
                        songs[int(song["id"])] = normalize_song(song)
            ids = list(songs)
        missing = [i for i in ids if i not in songs]
        warnings = []
        if missing:
            warnings.append(f"有 {len(missing)} 首歌曲详情不可用，可能已下架或没有访问权限。")
        try:
            count = int(raw.get("trackCount", len(ids)))
        except (TypeError, ValueError):
            count = len(ids)
        if count > len(ids):
            warnings.append(f"歌单标记 {count} 首，但接口只返回 {len(ids)} 个歌曲编号。")
        return {"playlist": {k: raw.get(k) for k in ("id", "name", "coverImgUrl", "trackCount", "description")},
                "songs": [songs[i] for i in ids if i in songs], "missing": missing, "warnings": warnings}

    def resolve(self, song_id, quality, fallback=False):
        reason = ""
        data = None
        try:
            data = self.call("/song/download/url/v1", {"id": song_id, "level": quality}).get("data")
        except UserError as e:
            reason = str(e)
        if not data or not data.get("url"):
            if fallback:
                endpoint = "/song/url" if quality == "dolby" else "/song/url/v1"
                params = {"id": song_id, "br": 999000, "immerseType": "c51"} if quality == "dolby" else {"id": song_id, "level": quality}
                items = self.call(endpoint, params).get("data") or []
                data = items[0] if items else None
            if not data or not data.get("url"):
                raise UserError(reason or "没有可用下载地址：可能需要登录、会员，或歌曲已下架。")
        if data.get("freeTrialInfo") or data.get("freeTrialPrivilege", {}).get("listenType") == 1:
            raise UserError("接口只返回试听片段，未下载为完整歌曲。")
        url = data["url"]
        if urlparse(url).scheme not in ("http", "https"):
            raise UserError("接口返回了不支持的下载地址")
        ext = str(data.get("type") or data.get("encodeType") or "mp3").lower()
        ext = {"mp4": "m4a", "mpeg": "mp3"}.get(ext, ext)
        if ext not in {"mp3", "flac", "m4a", "aac", "ogg", "opus", "wav"}:
            raise UserError(f"接口返回了不支持的音频类型：{ext}")
        actual = data.get("level") or (f"{int(data['br']) // 1000} kbps" if data.get("br") else "接口未报告")
        return {"url": url, "ext": ext, "actual_quality": actual, "size": int(data.get("size") or 0), "md5": data.get("md5")}

    def lyric(self, song_id):
        return self.call("/lyric/new", {"id": song_id})


def normalize_song(song):
    album = song.get("al") or song.get("album") or {}
    artists = song.get("ar") or song.get("artists") or []
    return dict(id=int(song["id"]), name=song.get("name") or "未命名歌曲",
                artists=" / ".join(a.get("name", "") for a in artists) or "未知歌手",
                album=album.get("name") or "未知专辑", cover=album.get("picUrl") or "",
                duration=int(song.get("dt") or song.get("duration") or 0))


_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def safe_name(value, limit=100):
    # Drop control characters plus Unicode bidi/format marks that could make a
    # file name render differently from what it actually is.
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f\x7f\u200b-\u200f\u202a-\u202e\u2066-\u2069]', "_", str(value)).strip(" .")
    name = name[:limit].rstrip(" .") or "未命名"
    # Windows maps "NUL.mp3" to the NUL device, so check the stem too.
    if name.split(".")[0].upper() in _RESERVED:
        name = "_" + name
    return name
