"""Auth endpoints: SPlayer session import, its on/off switch and the undo action."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app as app_module
from core import DEFAULTS


class SplayerLoginApiTests(unittest.TestCase):
    """走真实的 Flask 路由（演示模式，不联网、不碰真实凭据）。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="auth-api-tests-")
        self.addClassCleanup = self.temp.cleanup
        self.addCleanup(self.temp.cleanup)
        self.app = app_module.create_app(Path(self.temp.name) / "cfg", demo=True, port=36999)
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
        # 演示模式启动时自带登录态，先登出，才能验证「从未登录 → 读取 → 撤回回未登录」
        self.assertEqual(self.post("/api/auth/logout").status_code, 200)
        with mock.patch.object(app_module, "read_splayer_cookie", return_value="MUSIC_U=fake"):
            import_result = self.post("/api/auth/from-splayer").get_json()
        self.assertTrue(import_result["user"])
        undone = self.post("/api/auth/from-splayer/undo")
        self.assertEqual(undone.status_code, 200)
        self.assertIsNone(undone.get_json()["user"])
        # 演示模式的 profile() 永远返回演示用户，所以这里查真正被还原的会话文件
        import json
        session = json.loads((Path(self.temp.name) / "cfg" / "session.json").read_text(encoding="utf-8"))
        self.assertEqual(session["cookie"], "")

    def test_undo_restores_a_previous_account(self):
        """撤回要还原「上一个账号」，而不是简单地登出。"""
        self.post("/api/auth/logout")
        self.assertEqual(self.post("/api/auth/cookie", {"cookie": "x"}).status_code, 200)  # 演示模式：建立会话
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
        # 之后再走别的登录方式（演示模式直接建会话），撤回点必须失效
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


if __name__ == "__main__":
    unittest.main()
