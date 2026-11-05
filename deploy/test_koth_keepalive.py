import unittest

from koth_preview_gateway import with_game_keepalive


class GameKeepaliveTests(unittest.TestCase):
    def test_caps_only_private_game_transport_and_is_idempotent(self):
        original = '''game.example {
 reverse_proxy 10.0.0.1:8080 {
  header_up Host {http.request.host}
  transport http {
   response_header_timeout 160s
   keepalive 2m
  }
 }
 forward_auth https://msgctf.kr {
  transport http {
   response_header_timeout 12s
  }
 }
}
'''
        rendered = with_game_keepalive(original, "10.0.0.1:8080")
        self.assertIn("keepalive 4s", rendered)
        self.assertNotIn("keepalive 2m", rendered)
        self.assertEqual(rendered.split("forward_auth", 1)[1],
                         original.split("forward_auth", 1)[1])
        self.assertEqual(with_game_keepalive(rendered, "10.0.0.1:8080"), rendered)
        self.assertIn("header_up Host {http.request.host}", rendered)

    def test_empty_transport_gets_an_idle_limit_and_disabled_reuse_is_preserved(self):
        original = '''game.example {
 reverse_proxy 127.0.0.1:8080 {
 }
 reverse_proxy 127.0.0.1:8080 {
  transport http {
   keepalive off # explicitly disabled
  }
 }
}
'''
        rendered = with_game_keepalive(original, "127.0.0.1:8080")
        self.assertEqual(rendered.count("keepalive 4s"), 1)
        self.assertEqual(rendered.count("keepalive off"), 1)

    def test_malformed_or_inline_target_is_rejected(self):
        for text in ("reverse_proxy 10.0.0.1:8080 {", "reverse_proxy 10.0.0.1:8080 {}"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                with_game_keepalive(text, "10.0.0.1:8080")

    def test_duplicate_transport_is_rejected(self):
        text = '''reverse_proxy 10.0.0.1:8080 {
 transport http {
 }
 transport http {
 }
}
'''
        with self.assertRaises(ValueError):
            with_game_keepalive(text, "10.0.0.1:8080")
