"""Persistent song-level thread pool with verified, atomic file delivery."""
from __future__ import annotations

import copy
import hashlib
import os
import shutil
import threading
import time
import uuid
from collections import Counter
from pathlib import Path

import requests

from core import DEFAULTS, UserError, atomic_json, read_json, safe_name
from lyrics import merge_lyrics, write_tags

ACTIVE = {"resolving", "downloading", "tagging"}
FINISHED = {"completed", "skipped", "cancelled"}
# A name that has been produced or is about to be produced by some song.
CLAIMED = ACTIVE | {"queued", "completed", "skipped"}
MAX_JOBS = 5000


def path_key(value):
    """Case- and separator-insensitive path key.

    On Windows/NTFS two names differing only in case are the same file, so plain
    string comparison would treat an existing song as somebody else's file and
    silently skip it.
    """
    if not value:
        return ""
    try:
        return os.path.normcase(os.path.abspath(str(value)))
    except (OSError, ValueError):
        return os.path.normcase(str(value))


def is_file(value):
    if not value:
        return False
    try:
        return Path(value).is_file()
    except OSError:
        return False


class Cancelled(Exception):
    pass


def audio_filename(song, ext, disambiguate=False):
    """`歌名 - 歌手.ext`, with the song id only when two songs would collide."""
    base = f"{safe_name(song['name'], 45)} - {safe_name(song['artists'], 35)}"
    if disambiguate:
        base += f" [{song['id']}]"
    return f"{base}.{ext}"


class DownloadManager:
    def __init__(self, store, api):
        self.store, self.api = store, api
        self.file = store.directory / "downloads.json"
        self.condition = threading.Condition(threading.RLock())
        self.jobs = read_json(self.file, [])
        if not isinstance(self.jobs, list):
            self.jobs = []
        self.paused = False
        self.closed = False
        self.active = set()
        self.cancel_events = {}
        self.owners = {}
        known = read_json(self.file, [])
        self.jobs = [job for job in (known if isinstance(known, list) else []) if isinstance(job, dict)]
        for job in self.jobs:
            # Tolerate hand-edited or older records instead of failing per task.
            job["settings"] = DEFAULTS | (job.get("settings") or {})
            if job.get("status") in ACTIVE:
                job["status"] = "queued"
                job["error"] = "上次运行中断，点击继续后重新下载"
            job["speed"] = 0
        self._sweep_temporary_files()
        self.threads = [threading.Thread(target=self._worker, daemon=True, name=f"download-{i}") for i in range(12)]
        for t in self.threads:
            t.start()

    def _settings_for(self, job):
        return DEFAULTS | (job.get("settings") or {})

    def _folders(self):
        """Directories that a previous run may have left temporary files in."""
        seen = {Path(self.store.settings["download_dir"])}
        for job in self.jobs:
            key = job.get("target_key")
            if key:
                seen.add(Path(key).parent)
        return seen

    def _sweep_temporary_files(self):
        """Remove leftovers from a run that was killed before its finally block."""
        for folder in self._folders():
            try:
                for leftover in folder.glob(".shiyin-*"):
                    leftover.unlink(missing_ok=True)
            except OSError:
                continue

    def _save(self):
        atomic_json(self.file, self.jobs)

    def snapshot(self):
        with self.condition:
            jobs = copy.deepcopy(self.jobs)
            counts = Counter(j["status"] for j in jobs)
            for j in jobs:
                j.pop("settings", None)
                j.pop("song", None)
                j.pop("target_key", None)
            return dict(jobs=list(reversed(jobs)), summary=dict(total=len(jobs), queued=counts["queued"],
                        active=sum(counts[s] for s in ACTIVE), completed=counts["completed"],
                        failed=counts["failed"], paused=counts["queued"] if self.paused else 0,
                        cancelled=counts["cancelled"], skipped=counts["skipped"]),
                        paused=self.paused, workers=self.store.settings["workers"])

    def enqueue(self, playlist, songs):
        with self.condition:
            if len(self.jobs) + len(songs) > MAX_JOBS:
                raise UserError(f"队列最多保留 {MAX_JOBS} 个任务，请先清理已结束的任务")
            settings = dict(self.store.settings)
            existing_keys = set()
            added, existing = 0, 0
            for song in songs:
                folder = Path(settings["download_dir"])
                if settings["playlist_folder"]:
                    folder /= safe_name(playlist["name"], 65) + f" [{playlist['id']}]"
                key = path_key(folder / f"{song['id']}-{settings['quality']}")
                # Only an in-flight task or one that actually produced a file
                # blocks a new job; failed and cancelled ones must stay retryable
                # so changed settings can take effect.
                previous = next((j for j in reversed(self.jobs) if path_key(j.get("target_key")) == key and (
                    j["status"] in ACTIVE or j["status"] == "queued"
                    or (j["status"] in {"completed", "skipped"} and is_file(j.get("path"))))), None)
                if previous or key in existing_keys:
                    existing += 1
                    continue
                self.jobs.append(dict(id=uuid.uuid4().hex, song_id=song["id"], name=song["name"],
                                      artists=song["artists"], album=song["album"], playlist=playlist["name"],
                                      playlist_id=playlist["id"], status="queued", progress=0,
                                      downloaded=0, total=0, speed=0, quality=settings["quality"],
                                      actual_quality="", path="", error="", warnings=[], attempt=0,
                                      settings=settings, song=song, target_key=key))
                existing_keys.add(key)
                added += 1
            self._save()
            self.condition.notify_all()
            return dict(added=added, existing=existing, total=len(songs))

    def action(self, action, job_id=None):
        with self.condition:
            targets = self.jobs if job_id is None else [j for j in self.jobs if j["id"] == job_id]
            if job_id and not targets:
                raise UserError("下载任务不存在")
            if action == "pause" and job_id is None:
                self.paused = True
            elif action == "resume" and job_id is None:
                self.paused = False
            elif action in {"cancel", "cancel_all"}:
                for j in targets:
                    if j["status"] == "queued":
                        j["status"] = "cancelled"
                    elif j["status"] in ACTIVE:
                        event = self.cancel_events.get(j["id"])
                        if event:
                            event.set()
            elif action in {"retry", "retry_failed"}:
                for j in targets:
                    if j["status"] in {"failed", "cancelled"}:
                        self._adopt_current_settings(j)
                        j.update(status="queued", error="", warnings=[], progress=0, downloaded=0, speed=0, attempt=0)
            elif action == "clear_finished" and job_id is None:
                self.jobs = [j for j in self.jobs if j["status"] not in FINISHED]
            else:
                raise UserError("不支持的队列操作")
            self._save()
            self.condition.notify_all()
        return {"ok": True}

    def _name_taken_by_another_song(self, job, target, folder):
        """True when another song in this folder would use the same file name.

        Songs still waiting to resolve have no final path yet, so a shared title
        and artist in the same folder counts as a conflict too. Both songs then
        carry their id in the name, which keeps the result independent of which
        one happens to download first.
        """
        plain = (safe_name(job["song"]["name"], 45), safe_name(job["song"]["artists"], 35))
        target_key_value = path_key(target)
        folder_key = path_key(folder)
        for other in self.jobs:
            if other["song_id"] == job["song_id"] or other["status"] not in CLAIMED:
                continue
            if other.get("path") and path_key(other["path"]) == target_key_value:
                return True
            peer = other.get("song")
            if peer and path_key(Path(other.get("target_key") or ".").parent) == folder_key:
                if (safe_name(peer["name"], 45), safe_name(peer["artists"], 35)) == plain:
                    return True
        return False

    def notify_settings(self):
        with self.condition:
            self.condition.notify_all()

    def _adopt_current_settings(self, job):
        """A retry follows the settings in force now, so a fix actually applies."""
        settings = dict(self.store.settings)
        folder = Path(settings["download_dir"])
        if settings["playlist_folder"]:
            folder /= safe_name(job["playlist"], 65) + f" [{job['playlist_id']}]"
        job["settings"] = settings
        job["quality"] = settings["quality"]
        job["target_key"] = str(folder / f"{job['song_id']}-{settings['quality']}")

    def _update(self, job, persist=False, **values):
        with self.condition:
            job.update(values)
            if persist:
                self._save()

    def _worker(self):
        while True:
            try:
                with self.condition:
                    self.condition.wait_for(lambda: self.closed or (
                        not self.paused
                        and len(self.active) < self.store.settings["workers"]
                        and any(j["status"] == "queued" for j in self.jobs)))
                    if self.closed:
                        return
                    job = next((j for j in self.jobs if j["status"] == "queued"), None)
                    if job is None:
                        continue
                    settings = self._settings_for(job)
                    job["settings"] = settings
                    job["status"] = "resolving"
                    # Ownership token: a retry can re-claim this job before the old
                    # worker cleans up, and that cleanup must not remove the new
                    # owner's cancel event or double-count the active slot.
                    token = object()
                    self.owners[job["id"]] = token
                    self.active.add(job["id"])
                    event = self.cancel_events[job["id"]] = threading.Event()
                    self._save()
            except Exception:
                # Persistence or parsing trouble must not kill the worker.
                continue
            self._run_job(job, event, settings, token)

    def _run_job(self, job, event, settings, token):
        job_id = job["id"]
        try:
            for attempt in range(settings["retries"] + 1):
                try:
                    self._check(event)
                    self._update(job, status="resolving", attempt=attempt + 1, error="", downloaded=0, progress=0, warnings=[])
                    self._download(job, event)
                    break
                except Cancelled:
                    self._update(job, persist=True, status="cancelled", speed=0, error="已取消")
                    break
                except Exception as exc:
                    message = str(exc) if isinstance(exc, (UserError, OSError)) else "传输失败，请检查网络后重试"
                    if attempt < settings["retries"]:
                        self._update(job, error=f"{message}；稍后重试", speed=0)
                        if event.wait(min(2 ** attempt, 8)):
                            self._update(job, persist=True, status="cancelled", speed=0, error="已取消")
                            break
                    else:
                        self._update(job, persist=True, status="failed", speed=0, error=message)
        finally:
            with self.condition:
                if self.owners.get(job_id) is token:
                    self.owners.pop(job_id, None)
                    self.active.discard(job_id)
                    self.cancel_events.pop(job_id, None)
                self.condition.notify_all()

    @staticmethod
    def _check(event):
        if event.is_set():
            raise Cancelled()

    def _download(self, job, event):
        settings, song = job["settings"], job["song"]
        resource = self.api.resolve(song["id"], settings["quality"], settings["playback_fallback"])
        self._check(event)
        folder = Path(job["target_key"]).parent
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / audio_filename(song, resource["ext"])
        if self._name_taken_by_another_song(job, target, folder):
            # Same title and artist in one folder: both songs keep their id so
            # neither silently replaces the other.
            target = folder / audio_filename(song, resource["ext"], disambiguate=True)
        self._update(job, actual_quality=resource["actual_quality"], path=str(target))
        if target.exists():
            if target.stat().st_size == 0:
                raise UserError("目标存在零字节文件，请先移走该文件再重试")
            known = any(j.get("path") and path_key(j["path"]) == path_key(target) and j["song_id"] == job["song_id"]
                        and j["status"] in {"completed", "skipped"} for j in self.jobs)
            warning = ("目标文件已存在，保留原文件；未验证其内容。" if known else
                       "已存在同名文件，但它不是本工具的下载记录，已跳过以免覆盖；如需下载请先移走该文件。")
            self._update(job, persist=True, status="skipped", progress=100, warnings=[warning])
            return
        temporary = folder / f".shiyin-{job['id']}.{resource['ext']}"
        lyric_temp = folder / f".shiyin-{job['id']}.lrc"
        warnings = []
        try:
            self._transfer(resource, temporary, job, event, warnings)
            self._check(event)
            self._update(job, status="tagging", speed=0, progress=98)
            lyric = ""
            if settings["save_lrc"] or settings["embed_lyrics"]:
                try:
                    lyric = merge_lyrics(self.api.lyric(song["id"]), settings["translation"], settings["romanization"])
                    if not lyric:
                        warnings.append("该歌曲没有可用歌词")
                except Exception as e:
                    warnings.append(f"歌词获取失败：{e if isinstance(e, UserError) else '接口返回异常'}")
            cover = None
            if settings["cover"] and song.get("cover"):
                try:
                    with requests.get(song["cover"], timeout=(5, 12), stream=True, allow_redirects=True) as r:
                        r.raise_for_status()
                        chunks = bytearray()
                        for chunk in r.iter_content(65536):
                            self._check(event)
                            chunks.extend(chunk)
                            if len(chunks) > 8 * 1024 * 1024:
                                raise UserError("封面超过 8 MB")
                        cover = bytes(chunks)
                except Cancelled:
                    raise
                except Exception:
                    warnings.append("封面获取失败，已保留音频")
            self._check(event)
            try:
                write_tags(temporary, song, lyric, embed_lyrics=settings["embed_lyrics"], cover=cover)
            except Exception:
                warnings.append("音频标签写入失败；请使用独立 LRC 文件")
            self._check(event)
            if settings["save_lrc"] and lyric:
                lyric_temp.write_text(lyric + "\n", encoding="utf-8")
            try:
                self._commit(temporary, target)
            except FileExistsError:
                # Somebody created the target after the check above; keep theirs.
                warnings.append("落盘时发现同名文件（可能由其它程序创建），已保留原文件")
                self._update(job, persist=True, status="skipped", progress=100, warnings=warnings)
                return
            if lyric_temp.exists():
                try:
                    self._commit(lyric_temp, target.with_suffix(".lrc"))
                except FileExistsError:
                    warnings.append("同名 LRC 已存在，保留原歌词文件")
                except OSError:
                    warnings.append("独立 LRC 保存失败")
            self._update(job, persist=True, status="completed", progress=100, speed=0, error="", warnings=warnings)
        finally:
            temporary.unlink(missing_ok=True)
            lyric_temp.unlink(missing_ok=True)

    def _transfer(self, resource, target, job, event, warnings):
        digest = hashlib.md5()
        # Each worker owns its connection; cookies never go to the audio CDN.
        # No system proxy: the service is local and proxying audio is surprising.
        with requests.Session() as session:
            session.trust_env = False
            with session.get(resource["url"], stream=True, timeout=(10, 15),
                             headers={"User-Agent": "Shiyin/1.0", "Accept-Encoding": "identity"}) as response:
                response.raise_for_status()
                if response.status_code != 200:
                    raise UserError(f"音频服务器返回异常状态 {response.status_code}")
                content_type = response.headers.get("Content-Type", "").lower()
                if "text/" in content_type or "json" in content_type or "html" in content_type:
                    raise UserError("下载地址返回了文本或错误页面，不是音频")
                total = int(response.headers.get("Content-Length") or resource.get("size") or 0)
                downloaded, start, last = 0, time.monotonic(), 0
                self._update(job, status="downloading", total=total)
                with target.open("wb") as f:
                    for chunk in response.iter_content(64 * 1024):
                        self._check(event)
                        if not chunk:
                            continue
                        f.write(chunk)
                        digest.update(chunk)
                        downloaded += len(chunk)
                        now = time.monotonic()
                        if now - last >= .15:
                            self._update(job, downloaded=downloaded, progress=min(97, downloaded / total * 97) if total else 0,
                                         speed=downloaded / max(now - start, .001))
                            last = now
                    f.flush()
                    os.fsync(f.fileno())
        if not downloaded or (total and downloaded != total):
            raise UserError(f"文件传输不完整：收到 {downloaded} / {total} 字节")
        if resource.get("size") and downloaded != resource["size"]:
            raise UserError("下载文件大小与音乐接口报告不一致")
        if resource.get("md5") and digest.hexdigest().lower() != resource["md5"].lower():
            raise UserError("音频 MD5 校验不一致，请重试")
        if not total and not resource.get("size") and not resource.get("md5"):
            # Nothing to check against; say so instead of implying verification.
            warnings.append("接口未提供文件大小，无法校验完整性")
        self._update(job, downloaded=downloaded, total=downloaded)

    @staticmethod
    def _commit(temporary, target):
        """Move the finished file into place without ever overwriting.

        Hard linking fails on filesystems without hard links (exFAT, some network
        shares), where an exclusive create is the equivalent guarantee. Both paths
        raise FileExistsError when something appeared after the earlier check.
        """
        try:
            os.link(temporary, target)
        except FileExistsError:
            raise
        except OSError:
            with target.open("xb") as destination, temporary.open("rb") as source:
                shutil.copyfileobj(source, destination, 1024 * 1024)
        temporary.unlink(missing_ok=True)

    def close(self):
        # Do not take the worker lock here: a stuck filesystem write would block
        # shutdown forever. A separate flag plus events is enough.
        self.closed = True
        for event in list(self.cancel_events.values()):
            event.set()
        with self.condition:
            self.condition.notify_all()
        for thread in self.threads:
            thread.join(timeout=2)
        self._sweep_temporary_files()
