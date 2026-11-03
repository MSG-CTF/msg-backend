import unittest
from koth_jenga_session_gateway import guard_jenga_sessions

CID="d541ca2f-a8d4-5a99-aa41-23fec07ab5fa"
ORIGIN="https://jenga.example"
ORIGINAL='''jenga.example {
 route {
  @internal path /internal /internal/*
  respond @internal "" 404
  @qa remote_ip 192.0.2.1/32
  handle @qa {
   reverse_proxy 10.0.0.1:8080
  }
  @released {
   expression `int({time.now.unix}) >= 10 && int({time.now.unix}) < 20`
   file {
    root /armed
    try_files jenga
   }
  }
  handle @released {
   reverse_proxy 10.0.0.1:8080 {
    transport http {
     max_conns_per_host 8
    }
   }
  }
  respond "KOTH_NOT_OPEN" 403
 }
}
score.example {
 respond "" 403
}'''


class JengaSessionGatewayTests(unittest.TestCase):
    def test_only_protects_the_existing_released_game_branch(self):
        result=guard_jenga_sessions(ORIGINAL,CID,ORIGIN)
        prefix=ORIGINAL.split('  @released {')[0]
        self.assertTrue(result.startswith(prefix))
        self.assertTrue(result.endswith('score.example {\n respond "" 403\n}'))
        self.assertIn('int({time.now.unix}) >= 10 && int({time.now.unix}) < 20',result)
        self.assertIn('try_files jenga',result)
        self.assertIn('max_conns_per_host 8',result)
        self.assertIn('{http.request.cookie.__Host-msg_koth_preview} == ""',result)
        self.assertIn('not path / /auth /healthz /favicon.ico /static/* /assets/*',result)
        self.assertIn('header_up -Authorization',result)
        self.assertIn('jenga_authorize',result)
        self.assertLess(result.index('jenga_authorize'),result.rindex('reverse_proxy'))

    def test_repeated_upgrade_or_ambiguous_release_is_rejected(self):
        for original in [guard_jenga_sessions(ORIGINAL,CID,ORIGIN),ORIGINAL+ORIGINAL,ORIGINAL.replace('@released {','@missing {')]:
            with self.assertRaises(ValueError):guard_jenga_sessions(original,CID,ORIGIN)

    def test_invalid_destinations_are_rejected(self):
        for cid,origin in [('invalid',ORIGIN),(CID,'http://jenga.example'),(CID,ORIGIN+'/path')]:
            with self.assertRaises(ValueError):guard_jenga_sessions(ORIGINAL,cid,origin)
