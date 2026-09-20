"""Download queue naming, dedupe and skip behaviour, served from a local fixture."""

import hashlib
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from core import Store
from downloader import DownloadManager, audio_filename


class StubAPI:
    """Resolves every song to one local fixture file served over HTTP."""

    def __init__(self, base_url, sample: Path, ext="mp3"):
        self.base_url, self.sample, self.ext = base_url, sample, ext
        self.payload = sample.read_bytes()

    def resolve(self, song_id, quality, fallback=False):
        return dict(url=f"{self.base_url}/{self.sample.name}", ext=self.ext,
                    actual_quality="stub", size=len(self.payload),
                    md5=hashlib.md5(self.payload).hexdigest())

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
        self.manager = DownloadManager(self.store, StubAPI(self.base_url, self.media / "sample.mp3"))
        self.addCleanup(self.manager.close)

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


if __name__ == "__main__":
    unittest.main()
