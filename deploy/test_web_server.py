import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import web_server


class WebServerTests(unittest.TestCase):
    def test_login_pool_is_single_threaded_and_independent_of_api_pool(self):
        api, login = web_server.commands({"WEB_CONCURRENCY": "3", "GUNICORN_THREADS": "8"})
        self.assertIn("config.wsgi:application", api)
        self.assertIn("config.login_wsgi:application", login)
        self.assertEqual(api[api.index("--workers")+1], "3")
        self.assertEqual(api[api.index("--threads")+1], "8")
        self.assertEqual(login[login.index("--workers")+1], "2")
        self.assertEqual(login[login.index("--threads")+1], "1")
        self.assertNotEqual(api[api.index("--bind")+1], login[login.index("--bind")+1])

    def test_proxy_routes_only_the_login_path_to_login_workers(self):
        config = web_server.nginx_configuration(8080)
        login = config.split("location = /api/v1/auth/login {", 1)[1].split("}", 1)[0]
        self.assertIn("proxy_pass http://login_workers;", login)
        self.assertIn("proxy_request_buffering on;", login)
        self.assertIn("client_max_body_size 8k;", login)
        self.assertIn("location / { proxy_pass http://api_workers; }", config)
        self.assertIn("client_max_body_size 0;", config)
        self.assertIn("proxy_buffering off;", config)
        self.assertIn("proxy_request_buffering off;", config)

    def test_invalid_worker_count_is_rejected_before_launch(self):
        for count in ("0", "-1", "9", "not-a-number"):
            with self.subTest(count=count), self.assertRaises(ValueError):
                web_server.commands({"LOGIN_WEB_CONCURRENCY": count})

    @unittest.skipUnless(os.name == "posix", "Container runtime uses POSIX ownership")
    def test_runtime_rejects_a_symlink_without_changing_its_target(self):
        with tempfile.TemporaryDirectory() as base:
            root = Path(base)
            target = root/"target"
            target.mkdir(mode=0o700)
            link = root/"runtime"
            link.symlink_to(target, target_is_directory=True)
            with self.assertRaises(ValueError):
                web_server.prepare_runtime(link)
            self.assertTrue(target.is_dir())

    def test_shutdown_race_does_not_skip_other_children(self):
        first, second = Mock(), Mock()
        first.pid, second.pid = 101, 102
        first.poll.return_value = second.poll.return_value = None
        with patch("web_server.os.killpg", side_effect=[ProcessLookupError(), None], create=True) as kill:
            web_server.stop([first, second], grace=.01)
        self.assertEqual(kill.call_count, 2)
        first.wait.assert_called_once()
        second.wait.assert_called_once()
