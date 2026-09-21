"""Auth endpoints: SPlayer session import, its on/off switch and the undo action."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app as app_module
from core import DEFAULTS


class StubAuthAPI:
    """最小可用的接口替身：只实现认证相关方法，不联网。"""

    def __init__(self):
        self.user = None

    def available(self):
        return True

    def profile(self, cookie=None):
        return self.user

    def login(self, cookie):
        if not cookie:
            raise ValueError("empty cookie")
        self.user = {"userId": 501, "nickname": "测试账号", "avatarUrl": ""}
        return self.user

    def playlists(self):
        return []

    def playlist(self, playlist_id):
        raise AssertionError("这些用例不应该取歌单")

    def songs(self, song_ids):
        return [], list(song_ids)

    def resolve(self, *args, **kwargs):
        raise AssertionError("这些用例不应该解析下载地址")

    def lyric(self, song_id):
        return {}

    def call(self, *args, **kwargs):
        return {}


class SplayerLoginApiTests(unittest.TestCase):
    """走真实的 Flask 路由（注入替身接口，不联网、不碰真实凭据）。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="auth-api-tests-")
        self.addClassCleanup = self.temp.cleanup
        self.addCleanup(self.temp.cleanup)
        self.stub = StubAuthAPI()
        self.app = app_module.create_app(Path(self.temp.name) / "cfg", port=36999, api=self.stub)
        self.addCleanup(self.app.extensions["downloads"].close)
        self.client = self.app.test_client()
        self.csrf = self.client.get("/api/bootstrap").get_json()["csrf"]

    def post(self, path, payload=None):
        return self.client.post(path, json=payload or {}, headers={"X-CSRF-Token": self.csrf})

    def set_setting(self, **values):
        return self.client.patch("/api/settings", json=values, headers={"X-CSRF-Token": self.csrf})

    def test_default_allows_import_and_returns_undo_flag(self):
        with mock.patch.object(app_module, "read_splayer_cookie", return_value="MUSIC_U=fake"):
            body = self.post("/api/auth/from-splayer").get_json()
        self.assertTrue(body["can_undo"])
        # 读不到 cookie 时安静失败（始终 mock，绝不碰真实的 SPlayer 配置）
        with mock.patch.object(app_module, "read_splayer_cookie", return_value=""):
            self.assertEqual(self.post("/api/auth/from-splayer").status_code, 400)

    def test_switch_off_hides_the_capability_even_with_a_readable_cookie(self):
        self.assertEqual(self.set_setting(splayer_login=False).status_code, 200)
        with mock.patch.object(app_module, "read_splayer_cookie", return_value="MUSIC_U=fake"):
            response = self.post("/api/auth/from-splayer")
        self.assertEqual(response.status_code, 400)
        self.assertIn("关闭", response.get_json()["error"])

    def test_undo_restores_the_previous_logged_out_state(self):
        self.assertEqual(self.post("/api/auth/logout").status_code, 200)   # 从未登录开始
        with mock.patch.object(app_module, "read_splayer_cookie", return_value="MUSIC_U=fake"):
            import_result = self.post("/api/auth/from-splayer").get_json()
        self.assertTrue(import_result["user"])
        undone = self.post("/api/auth/from-splayer/undo")
        self.assertEqual(undone.status_code, 200)
        self.assertIsNone(undone.get_json()["user"])
        # 查真正被还原的会话文件（它才是持久化的状态）
        import json
        session = json.loads((Path(self.temp.name) / "cfg" / "session.json").read_text(encoding="utf-8"))
        self.assertEqual(session["cookie"], "")

    def test_undo_restores_a_previous_account(self):
        """撤回要还原「上一个账号」，而不是简单地登出。"""
        self.assertEqual(self.post("/api/auth/cookie", {"cookie": "x"}).status_code, 200)  # 先登录一个账号
        before = self.client.get("/api/session").get_json()["user"]
        self.assertTrue(before)
        with mock.patch.object(app_module, "read_splayer_cookie", return_value="MUSIC_U=fake"):
            self.post("/api/auth/from-splayer")
        undone = self.post("/api/auth/from-splayer/undo").get_json()
        self.assertEqual(undone["user"], before)

    def test_undo_is_single_shot(self):
        with mock.patch.object(app_module, "read_splayer_cookie", return_value="MUSIC_U=fake"):
            self.post("/api/auth/from-splayer")
        self.assertEqual(self.post("/api/auth/from-splayer/undo").status_code, 200)
        self.assertEqual(self.post("/api/auth/from-splayer/undo").status_code, 400)

    def test_undo_without_a_prior_import_is_refused(self):
        self.assertEqual(self.post("/api/auth/from-splayer/undo").status_code, 400)

    def test_other_logins_invalidate_the_undo_point(self):
        with mock.patch.object(app_module, "read_splayer_cookie", return_value="MUSIC_U=fake"):
            self.post("/api/auth/from-splayer")
        # 之后再走别的登录方式，撤回点必须失效
        self.assertEqual(self.post("/api/auth/cookie", {"cookie": "x"}).status_code, 200)
        self.assertEqual(self.post("/api/auth/from-splayer/undo").status_code, 400)

    def test_settings_round_trip_and_validation(self):
        saved = self.set_setting(splayer_login=False).get_json()
        self.assertFalse(saved["splayer_login"])
        self.assertFalse(self.client.get("/api/settings").get_json()["splayer_login"])
        # 类型错误要被拒
        self.assertEqual(self.set_setting(splayer_login="yes").status_code, 400)
        self.assertEqual(self.set_setting(splayer_login=True).get_json()["splayer_login"], True)

    def test_new_setting_participates_in_startup_validation(self):
        """手改 settings.json 也要受同一套校验约束。"""
        self.assertIn("splayer_login", DEFAULTS)
        config = Path(self.temp.name) / "cfg" / "settings.json"
        config.write_text('{"splayer_login": "yes"}', encoding="utf-8")
        from core import Store
        self.assertEqual(Store(Path(self.temp.name) / "cfg").settings["splayer_login"], DEFAULTS["splayer_login"])

    def test_writes_still_require_csrf(self):
        self.assertEqual(self.client.post("/api/auth/from-splayer", json={}).status_code, 403)
        self.assertEqual(self.client.post("/api/auth/from-splayer/undo", json={}).status_code, 403)

    def test_saved_session_is_restored_on_bootstrap(self):
        """重启后磁盘上还有会话时，界面不该显示成未登录。"""
        import json
        config = Path(self.temp.name) / "resumed-cfg"
        config.mkdir()
        # 会话文件必须在实例化之前就位：Store 是在构造时读它的
        (config / "session.json").write_text(json.dumps({"cookie": "MUSIC_U=saved"}), encoding="utf-8")
        stub = StubAuthAPI()
        stub.user = {"userId": 777, "nickname": "已保存的账号", "avatarUrl": ""}
        restarted = app_module.create_app(config, port=36998, api=stub)
        self.addCleanup(restarted.extensions["downloads"].close)
        body = restarted.test_client().get("/api/bootstrap").get_json()
        self.assertEqual(body["user"]["nickname"], "已保存的账号")

    def test_without_a_cookie_bootstrap_does_not_probe(self):
        self.stub.user = {"userId": 777, "nickname": "不该出现", "avatarUrl": ""}
        self.assertIsNone(self.client.get("/api/bootstrap").get_json()["user"])


if __name__ == "__main__":
    unittest.main()
