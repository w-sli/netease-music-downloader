"""Settings validation, file-name hardening and API client behaviour."""

import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from core import DEFAULTS, Store, UserError, safe_name, Netease


class SafeNameTests(unittest.TestCase):
    def test_path_separators_and_traversal_are_neutralised(self):
        for hostile in ("../../etc/passwd", "..\\..\\windows\\win.ini", "C:\\Windows\\system32",
                        "/etc/shadow", "a/b", "a\\b", "....//....//x"):
            with self.subTest(value=hostile):
                name = safe_name(hostile)
                self.assertNotIn("/", name)
                self.assertNotIn("\\", name)
                self.assertNotIn(":", name)
                # No leading dot, so ".." and hidden names cannot survive.
                self.assertFalse(name.startswith("."))

    def test_alternate_data_stream_and_control_characters(self):
        self.assertNotIn(":", safe_name("name:stream"))
        self.assertEqual(safe_name("a\x00b\x1fc"), "a_b_c")

    def test_windows_reserved_stem_is_escaped(self):
        # Windows maps NUL.mp3 to the NUL device, so the stem must be checked.
        self.assertEqual(safe_name("NUL.mp3"), "_NUL.mp3")
        self.assertEqual(safe_name("con.txt"), "_con.txt")
        self.assertEqual(safe_name("COM1"), "_COM1")
        self.assertEqual(safe_name("lpt9.tar.gz"), "_lpt9.tar.gz")
        self.assertEqual(safe_name("console"), "console")

    def test_bidi_and_zero_width_marks_are_removed(self):
        self.assertEqual(safe_name("a\u202eb"), "a_b")
        self.assertEqual(safe_name("a\u200bb"), "a_b")

    def test_empty_and_dotted_names_fall_back(self):
        self.assertEqual(safe_name("   "), "未命名")
        self.assertEqual(safe_name("..."), "未命名")
        self.assertEqual(safe_name(""), "未命名")

    def test_truncation_happens_after_sanitising(self):
        self.assertEqual(safe_name("x" * 200, limit=10), "x" * 10)
        # A trailing separator must not survive as the last character.
        self.assertEqual(safe_name("abc/def", limit=4), "abc_")


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="store-tests-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_tampered_settings_are_repaired_on_load(self):
        """A hand-edited settings.json must not bypass save-time constraints."""
        directory = self.root / "cfg"
        directory.mkdir()
        (directory / "settings.json").write_text(json.dumps({
            "api_base": "http://evil.example.com",
            "download_dir": "/tmp",
            "workers": 999,
            "quality": "not-a-quality",
        }), encoding="utf-8")
        store = Store(directory)
        self.assertEqual(store.settings["api_base"], DEFAULTS["api_base"])
        self.assertEqual(store.settings["workers"], DEFAULTS["workers"])
        self.assertEqual(store.settings["quality"], DEFAULTS["quality"])
        self.assertEqual(store.settings["download_dir"], DEFAULTS["download_dir"])

    def test_unknown_keys_in_settings_file_are_dropped(self):
        directory = self.root / "cfg2"
        directory.mkdir()
        (directory / "settings.json").write_text(json.dumps({"workers": 3, "backdoor": True}), encoding="utf-8")
        store = Store(directory)
        self.assertEqual(store.settings["workers"], 3)
        self.assertNotIn("backdoor", store.settings)

    def test_corrupt_settings_fall_back_to_defaults(self):
        directory = self.root / "cfg3"
        directory.mkdir()
        (directory / "settings.json").write_text("{not json", encoding="utf-8")
        self.assertEqual(Store(directory).settings, dict(DEFAULTS))

    def test_update_rejects_wrong_types_without_crashing(self):
        store = Store(self.root / "cfg4")
        for bad in (123, None, 1.5, ["http://127.0.0.1"]):
            with self.subTest(value=bad):
                with self.assertRaises(UserError):
                    store.update({"api_base": bad})
        for bad in ("12", 1.5, True, None):
            with self.subTest(workers=bad):
                with self.assertRaises(UserError):
                    store.update({"workers": bad})

    def test_update_rejects_remote_and_credentialed_api_bases(self):
        store = Store(self.root / "cfg5")
        for bad in ("http://evil.example.com", "http://127.0.0.1@evil.com",
                    "https://127.0.0.1:25884", "http://127.0.0.1:25884/?x=1",
                    "http://user:pw@127.0.0.1:25884"):
            with self.subTest(value=bad):
                with self.assertRaises(UserError):
                    store.update({"api_base": bad})

    def test_loopback_api_bases_are_accepted(self):
        store = Store(self.root / "cfg6")
        for good in ("http://127.0.0.1:25884", "http://localhost:3000",
                     "http://127.0.0.1:25884/api/netease", "http://[::1]:3000"):
            with self.subTest(value=good):
                self.assertTrue(store.update({"api_base": good})["api_base"].startswith(good[:14]))


class _RedirectHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(302)
        self.send_header("Location", "http://127.0.0.1:1/elsewhere")
        self.end_headers()

    def do_POST(self):
        self.do_GET()

    def log_message(self, *args):
        pass


class ApiClientTests(unittest.TestCase):
    """The account cookie must never follow a redirect off the loopback host."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="api-tests-")
        self.addCleanup(self.temp.cleanup)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _RedirectHandler)
        self.addCleanup(self.server.server_close)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.store = Store(Path(self.temp.name) / "cfg")
        self.store.settings["api_base"] = "http://127.0.0.1:%d" % self.server.server_address[1]
        self.api = Netease(self.store)

    def test_redirect_is_refused_instead_of_forwarding_cookie(self):
        with self.assertRaises(UserError) as error:
            self.api.call("/login/status")
        self.assertIn("重定向", str(error.exception))

    def test_available_reports_false_on_redirect(self):
        self.assertFalse(self.api.available())


class _FakeNetease(Netease):
    """Feeds canned payloads through the real response-shape handling."""

    def __init__(self, store, payloads):
        super().__init__(store)
        self.payloads = payloads

    def call(self, endpoint, params=None, cookie=None, allow_codes=()):
        return self.payloads[endpoint]


class PlaylistParsingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="playlist-tests-")
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / "cfg")

    def test_malformed_tracks_do_not_raise_key_errors(self):
        api = _FakeNetease(self.store, {
            "/playlist/detail": {"playlist": {"id": 1, "name": "x", "trackCount": 3,
                                              "trackIds": [{"id": "not-a-number"}, {}, "junk", {"id": 42}]}},
            "/song/detail": {"songs": [{"id": 42, "name": "ok", "ar": [], "al": {}}]},
        })
        result = api.playlist(1)
        self.assertEqual([s["id"] for s in result["songs"]], [42])

    def test_non_dict_playlist_is_rejected(self):
        api = _FakeNetease(self.store, {"/playlist/detail": {"playlist": ["nope"]}})
        with self.assertRaises(UserError):
            api.playlist(1)

    def test_playlist_list_skips_entries_without_id(self):
        api = _FakeNetease(self.store, {
            "/login/status": {"data": {"profile": {"userId": 7, "nickname": "me"}}},
            "/user/playlist": {"playlist": [{"id": 1, "name": "a"}, {"name": "no id"}, "junk"]},
        })
        result = api.playlists()
        self.assertEqual([p["id"] for p in result], [1])


if __name__ == "__main__":
    unittest.main()
