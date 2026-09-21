"""Download queue naming, dedupe, skip and commit behaviour (local fixtures only)."""

import hashlib
import os
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from core import Store
from downloader import DownloadManager, audio_filename, cover_url, path_key


class StubAPI:
    """Resolves every song to one local fixture file served over HTTP."""

    def __init__(self, base_url, sample: Path, ext="mp3", delay=0.0, report_size=True):
        self.base_url, self.sample, self.ext = base_url, sample, ext
        self.delay = delay
        self.report_size = report_size
        self.payload = sample.read_bytes()

    def resolve(self, song_id, quality, fallback=False):
        return dict(url=f"{self.base_url}/{self.sample.name}", ext=self.ext,
                    actual_quality="stub",
                    size=len(self.payload) if self.report_size else 0,
                    md5=hashlib.md5(self.payload).hexdigest() if self.report_size else None)

    def lyric(self, song_id):
        return {"lrc": {"lyric": "[00:00.000]第一行\n[00:02.000]第二行"},
                "tlyric": {"lyric": "[00:00.100]first line\n[00:02.050]second line"}}


def song(song_id, name="同一首歌", artists="同一位歌手"):
    return dict(id=song_id, name=name, artists=artists, album="测试专辑",
                cover="", duration=5000)


class QueueNamingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="queue-tests-")
        cls.addClassCleanup(cls.temp.cleanup)
        cls.root = Path(cls.temp.name)
        cls.media = cls.root / "media"
        cls.media.mkdir()
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg is None:
            raise RuntimeError("These tests require ffmpeg")
        subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                        "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                        "-c:a", "libmp3lame", str(cls.media / "sample.mp3")],
                       check=True, capture_output=True, timeout=30)
        handler = lambda *a, **k: SimpleHTTPRequestHandler(*a, directory=str(cls.media), **k)
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        cls.addClassCleanup(cls.server.server_close)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base_url = "http://127.0.0.1:%d" % cls.server.server_address[1]

    def setUp(self):
        self.out = self.root / self._testMethodName
        self.store = Store(self.root / (self._testMethodName + "-cfg"))
        self.store.settings["download_dir"] = str(self.out)
        self.store.settings["playlist_folder"] = False
        self.store.settings["workers"] = 3
        self.api = StubAPI(self.base_url, self.media / "sample.mp3")
        self.manager = DownloadManager(self.store, self.api)
        self.addCleanup(self.manager.close)

    def build_manager(self, api=None, workers=3):
        self.store.settings["workers"] = workers
        manager = DownloadManager(self.store, api or self.api)
        self.addCleanup(manager.close)
        return manager

    def run_queue(self, playlist, songs, timeout=60):
        self.manager.enqueue(playlist, songs)
        deadline = time.time() + timeout
        while time.time() < deadline:
            snap = self.manager.snapshot()
            if snap["summary"]["active"] == 0 and snap["summary"]["queued"] == 0:
                return snap
            time.sleep(0.1)
        self.fail("queue did not settle in time")

    def audio_files(self):
        return sorted(p.name for p in self.out.glob("*.mp3"))

    def playlist(self, name="测试歌单", playlist_id=1):
        return dict(id=playlist_id, name=name)

    # ---------- 命名 ----------
    def test_plain_name_is_song_and_artist(self):
        snap = self.run_queue(self.playlist(), [song(1001, "海边的慢镜头", "某歌手")])
        self.assertEqual(snap["jobs"][0]["status"], "completed")
        self.assertEqual(self.audio_files(), ["海边的慢镜头 - 某歌手.mp3"])
        self.assertTrue((self.out / "海边的慢镜头 - 某歌手.lrc").is_file())

    def test_illegal_characters_are_replaced(self):
        name = audio_filename(dict(id=1, name='a/b:c*d?e"f<g>h|i', artists="x\\y"), "mp3")
        self.assertEqual(name, "a_b_c_d_e_f_g_h_i - x_y.mp3")

    def test_blank_name_falls_back_and_is_truncated(self):
        long_name = audio_filename(dict(id=1, name="歌" * 80, artists="   "), "mp3")
        self.assertEqual(long_name, "歌" * 45 + " - 未命名.mp3")

    def test_same_title_and_artist_are_disambiguated_by_id(self):
        snap = self.run_queue(self.playlist(), [song(1001), song(1002)])
        self.assertEqual(snap["summary"]["completed"], 2, "同名歌曲都必须下载")
        self.assertEqual(self.audio_files(),
                         ["同一首歌 - 同一位歌手 [1001].mp3", "同一首歌 - 同一位歌手 [1002].mp3"])

    def test_disambiguation_leaves_other_songs_untouched(self):
        snap = self.run_queue(self.playlist(), [song(1001), song(1002, "另一首", "另一位")])
        self.assertEqual(snap["summary"]["completed"], 2)
        self.assertIn("同一首歌 - 同一位歌手.mp3", self.audio_files())
        self.assertIn("另一首 - 另一位.mp3", self.audio_files())

    # ---------- 已有检查 ----------
    def test_second_run_does_not_download_again(self):
        self.run_queue(self.playlist(), [song(1001)])
        path = self.out / "同一首歌 - 同一位歌手.mp3"
        before = path.read_bytes()
        # The enqueue layer recognises the finished job whose file is still on
        # disk, so no second task is even created.
        result = self.manager.enqueue(self.playlist(), [song(1001)])
        self.assertEqual((result["added"], result["existing"]), (0, 1))
        snap = self.manager.snapshot()
        self.assertEqual(snap["summary"]["total"], 1)
        self.assertEqual(self.audio_files(), ["同一首歌 - 同一位歌手.mp3"])
        self.assertEqual(path.read_bytes(), before, "已有文件不应被重写")

    def test_enqueue_reports_existing_while_file_is_present(self):
        self.run_queue(self.playlist(), [song(1001)])
        result = self.manager.enqueue(self.playlist(), [song(1001)])
        self.assertEqual((result["added"], result["existing"]), (0, 1))

    def test_deleted_file_can_be_downloaded_again(self):
        self.run_queue(self.playlist(), [song(1001)])
        (self.out / "同一首歌 - 同一位歌手.mp3").unlink()
        result = self.manager.enqueue(self.playlist(), [song(1001)])
        self.assertEqual(result["added"], 1, "文件已删除时应允许重新下载")

    def test_untracked_same_named_file_is_kept_and_reported(self):
        self.out.mkdir(parents=True, exist_ok=True)
        foreign = self.out / "同一首歌 - 同一位歌手.mp3"
        foreign.write_bytes(b"user's own file")
        snap = self.run_queue(self.playlist(), [song(1001)])
        job = snap["jobs"][0]
        self.assertEqual(job["status"], "skipped")
        self.assertEqual(foreign.read_bytes(), b"user's own file", "不能覆盖用户的同名文件")
        self.assertTrue(any("不是本工具的下载记录" in w for w in job["warnings"]), job["warnings"])

    def test_zero_byte_file_is_refused_not_skipped(self):
        self.out.mkdir(parents=True, exist_ok=True)
        (self.out / "同一首歌 - 同一位歌手.mp3").write_bytes(b"")
        snap = self.run_queue(self.playlist(), [song(1001)])
        job = snap["jobs"][0]
        self.assertEqual(job["status"], "failed")
        self.assertIn("零字节", job["error"])

    def test_failed_job_does_not_block_a_new_attempt(self):
        self.store.settings["download_dir"] = str(self.out)
        self.manager.api.resolve = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
        snap = self.run_queue(self.playlist(), [song(1001)])
        self.assertEqual(snap["summary"]["failed"], 1)
        self.manager.api.resolve = StubAPI(self.base_url, self.media / "sample.mp3").resolve
        result = self.manager.enqueue(self.playlist(), [song(1001)])
        self.assertEqual(result["added"], 1, "失败任务不应挡住重新入队")

    # ---------- 音质与标签 ----------
    def test_quality_change_keeps_the_existing_file(self):
        self.run_queue(self.playlist(), [song(1001)])
        self.store.settings["quality"] = "lossless"
        snap = self.run_queue(self.playlist(), [song(1001)])
        job = snap["jobs"][0]
        self.assertEqual(job["status"], "skipped")
        self.assertEqual(self.audio_files(), ["同一首歌 - 同一位歌手.mp3"], "不应产生重复或覆盖")

    def test_tags_and_lyrics_are_written(self):
        self.run_queue(self.playlist(), [song(1001)])
        import mutagen
        audio = mutagen.File(self.out / "同一首歌 - 同一位歌手.mp3")
        self.assertEqual(audio.tags["TIT2"].text, ["同一首歌"])
        self.assertEqual(audio.tags["TPE1"].text, ["同一位歌手"])
        self.assertEqual(audio.tags["TALB"].text, ["测试专辑"])
        lyrics = str(audio.tags.get("USLT::und"))
        self.assertIn("第一行", lyrics)
        self.assertIn("first line", lyrics, "翻译应合并进内嵌歌词")

    # ---------- 落盘原语：硬链接不可用 / 冲突 ----------
    def test_falls_back_when_hard_links_are_unavailable(self):
        """exFAT and some network shares have no hard links; copying must work."""
        with mock.patch("os.link", side_effect=OSError(1, "hard links unsupported")):
            snap = self.run_queue(self.playlist(), [song(1001)])
        self.assertEqual(snap["jobs"][0]["status"], "completed")
        self.assertEqual(self.audio_files(), ["同一首歌 - 同一位歌手.mp3"])
        self.assertFalse(list(self.out.glob(".shiyin-*")), "临时文件应已清理")

    def test_fallback_never_overwrites_an_existing_file(self):
        self.out.mkdir(parents=True, exist_ok=True)
        foreign = self.out / "同一首歌 - 同一位歌手.mp3"
        foreign.write_bytes(b"user data")

        def conflict(*args, **kwargs):
            raise FileExistsError(17, "exists")

        with mock.patch("os.link", side_effect=conflict):
            snap = self.run_queue(self.playlist(), [song(1001)])
        self.assertEqual(snap["jobs"][0]["status"], "skipped")
        self.assertEqual(foreign.read_bytes(), b"user data")

    def test_conflict_appearing_after_the_check_is_kept(self):
        """A file created between the exists() check and the commit must win."""
        original_commit = DownloadManager._commit

        def racing_commit(temporary, target):
            target.write_bytes(b"raced in")
            return original_commit(temporary, target)

        with mock.patch.object(DownloadManager, "_commit", staticmethod(racing_commit)):
            snap = self.run_queue(self.playlist(), [song(1001)])
        job = snap["jobs"][0]
        self.assertEqual(job["status"], "skipped")
        self.assertEqual((self.out / "同一首歌 - 同一位歌手.mp3").read_bytes(), b"raced in")
        self.assertTrue(any("同名文件" in w for w in job["warnings"]), job["warnings"])

    # ---------- 取消与重试 ----------
    def test_cancel_then_retry_leaves_no_stale_owner(self):
        """Regression: the old worker must not free the new worker's slot."""
        slow = StubAPI(self.base_url, self.media / "sample.mp3", delay=0.02)
        manager = self.build_manager(api=slow, workers=1)
        manager.enqueue(self.playlist(), [song(1001)])
        for _ in range(3):
            deadline = time.time() + 10
            while time.time() < deadline:
                snap = manager.snapshot()
                if snap["summary"]["active"]:
                    job_id = snap["jobs"][0]["id"]
                    manager.action("cancel", job_id)
                    manager.action("retry", job_id)
                    break
                time.sleep(0.02)
            for _ in range(300):
                if not manager.snapshot()["summary"]["active"]:
                    break
                time.sleep(0.02)
        snap = manager.snapshot()
        self.assertEqual(snap["summary"]["active"], 0)
        self.assertEqual(manager.active, set(), "并发槽位不应泄漏")
        self.assertEqual(manager.owners, {}, "所有权令牌不应残留")
        self.assertFalse(list(self.out.glob(".shiyin-*")), "临时文件应已清理")

    def test_leftover_temporary_files_are_swept_on_start(self):
        self.out.mkdir(parents=True, exist_ok=True)
        stale = self.out / ".shiyin-deadbeef.mp3"
        stale.write_bytes(b"partial")
        self.build_manager()
        self.assertFalse(stale.exists(), "启动时应清理上次中断留下的临时文件")

    # ---------- 大小写不敏感文件系统（NTFS/exFAT） ----------
    def test_case_insensitive_collision_is_not_treated_as_foreign(self):
        """On NTFS, "Song - A" and "song - a" are the same file."""
        with mock.patch("os.path.normcase", side_effect=lambda value: str(value).lower()):
            first = self.run_queue(self.playlist(), [song(1001, "Song", "Artist")])
            self.assertEqual(first["jobs"][0]["status"], "completed")
            second = self.run_queue(self.playlist(), [song(1001, "Song", "Artist")])
            # Same song id: recognised as our own record, not as a foreign file.
            self.assertIn(second["jobs"][0]["status"], {"skipped", "completed"})
            self.assertFalse(
                any("不是本工具的下载记录" in w for w in second["jobs"][0]["warnings"]),
                second["jobs"][0]["warnings"])

    def test_path_key_is_case_and_separator_insensitive(self):
        with mock.patch("os.path.normcase", side_effect=lambda value: str(value).replace("\\", "/").lower()):
            self.assertEqual(path_key("C:\\Music\\A.mp3"), path_key("c:/music/a.mp3"))

    # ---------- 封面尺寸 ----------
    def test_cover_url_asks_the_cdn_for_a_resized_image(self):
        """A bare picUrl can serve a multi-megabyte original, bloating every file."""
        self.assertEqual(cover_url("https://p1.music.126.net/abc.jpg"),
                         "https://p1.music.126.net/abc.jpg?param=1024y1024")
        # 已有查询串时必须用 & 追加
        self.assertEqual(cover_url("https://p1.music.126.net/abc.jpg?x=1"),
                         "https://p1.music.126.net/abc.jpg?x=1&param=1024y1024")
        self.assertEqual(cover_url(""), "")
        self.assertIn("param=512y512", cover_url("https://p1.music.126.net/a.jpg", size=512))

    # ---------- 单曲下载（不依赖歌单） ----------
    def test_single_song_lands_in_the_download_root(self):
        """Single downloads have no playlist, so they must not use a subfolder."""
        self.store.settings["playlist_folder"] = True
        manager = self.build_manager()
        manager.enqueue(dict(id=0, name="单曲下载"), [song(2001, "单曲一", "歌手甲")],
                        use_playlist_folder=False)
        deadline = time.time() + 30
        while time.time() < deadline:
            snap = manager.snapshot()
            if not snap["summary"]["active"] and not snap["summary"]["queued"]:
                break
            time.sleep(0.05)
        job = snap["jobs"][0]
        self.assertEqual(job["status"], "completed")
        self.assertTrue(job["single"])
        self.assertEqual(Path(job["path"]).parent, self.out, "单曲应直接落在下载根目录")
        self.assertEqual(self.audio_files(), ["单曲一 - 歌手甲.mp3"])

    def test_single_song_retry_keeps_the_same_folder(self):
        """A retry must not silently move the file into a playlist folder."""
        self.store.settings["playlist_folder"] = True
        manager = self.build_manager()
        manager.enqueue(dict(id=0, name="单曲下载"), [song(2001, "单曲一", "歌手甲")],
                        use_playlist_folder=False)
        deadline = time.time() + 30
        while time.time() < deadline:
            if not manager.snapshot()["summary"]["active"]:
                break
            time.sleep(0.05)
        job_id = manager.snapshot()["jobs"][0]["id"]
        manager.action("cancel", job_id)
        manager.action("retry", job_id)
        job = next(j for j in manager.jobs if j["id"] == job_id)
        self.assertEqual(Path(job["target_key"]).parent, self.out,
                         "重试后仍应指向下载根目录，而不是“单曲下载 [0]”子目录")

    def test_playlist_download_still_uses_its_folder(self):
        self.store.settings["playlist_folder"] = True
        manager = self.build_manager()
        manager.enqueue(self.playlist("我的歌单", 77), [song(2002, "歌单一", "歌手乙")])
        job = manager.jobs[0]
        self.assertEqual(Path(job["target_key"]).parent.name, "我的歌单 [77]")
        self.assertFalse(job["single"])


class NoLengthHandler(BaseHTTPRequestHandler):
    """Serves the fixture without Content-Length (HTTP/1.0 close-delimited)."""

    payload = b""

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "audio/mpeg")
        self.end_headers()
        self.wfile.write(type(self).payload)

    def log_message(self, *args):
        pass


class NoContentLengthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="nolength-tests-")
        cls.addClassCleanup(cls.temp.cleanup)
        cls.root = Path(cls.temp.name)
        NoLengthHandler.payload = b"ID3" + b"\x00" * 4096
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), NoLengthHandler)
        cls.addClassCleanup(cls.server.server_close)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    def test_download_without_content_length_notes_unverified_integrity(self):
        store = Store(self.root / "cfg")
        store.settings["download_dir"] = str(self.root / "out")
        store.settings["playlist_folder"] = False
        store.settings["save_lrc"] = False
        store.settings["embed_lyrics"] = False
        store.settings["cover"] = False

        class Api:
            def resolve(self, song_id, quality, fallback=False):
                return dict(url="http://127.0.0.1:%d/x.mp3" % self.server_port, ext="mp3",
                            actual_quality="stub", size=0, md5=None)

            def lyric(self, song_id):
                return {}

        api = Api()
        api.server_port = self.server.server_address[1]
        manager = DownloadManager(store, api)
        self.addCleanup(manager.close)
        manager.enqueue(dict(id=1, name="t"), [song(1001, "无名", "无姓")])
        deadline = time.time() + 30
        while time.time() < deadline:
            snap = manager.snapshot()
            if not snap["summary"]["active"] and not snap["summary"]["queued"]:
                break
            time.sleep(0.05)
        job = snap["jobs"][0]
        self.assertEqual(job["status"], "completed")
        self.assertTrue(any("无法校验完整性" in w for w in job["warnings"]), job["warnings"])


if __name__ == "__main__":
    unittest.main()
