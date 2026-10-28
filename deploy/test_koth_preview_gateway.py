from pathlib import Path
import unittest
from koth_preview_gateway import render_preview_gateway, upgrade_game_preview, GAME_PROFILES


class PreviewGatewayTests(unittest.TestCase):
    def test_adds_authentication_without_weakening_public_or_score_routes(self):
        original='''game.example {
 route {
  @internal path /internal /internal/* /__operator /__operator/*
  respond @internal "" 404
  @qa remote_ip 192.0.2.1/32
  handle @qa {
   reverse_proxy 10.0.0.1:8080 {
   }
  }
  @released expression `some_existing_window`
  handle @released {
   reverse_proxy 10.0.0.1:8080 {
   }
  }
  respond "KOTH_NOT_OPEN" 403
 }
}
score.example {
 respond "" 403
}'''
        bootstrap=(Path(__file__).parent/'koth-admin-preview.html').read_text(encoding='utf-8')
        rendered=render_preview_gateway(original,'00000000-0000-4000-8000-000000000001','https://game.example','10.0.0.1:8080',bootstrap)
        self.assertIn('@released expression `some_existing_window`',rendered)
        self.assertIn('respond @internal "" 404',rendered)
        self.assertTrue(rendered.endswith('score.example {\n respond "" 403\n}'))
        self.assertEqual(rendered.count('header_up Cookie "(^|; *)__Host-msg_koth_preview=[^;]*" ""'),3)
        self.assertIn('forward_auth https://msgctf.kr',rendered)
        self.assertIn("script-src 'sha256-",rendered)
        self.assertIn('max_size 4KB',rendered)
        self.assertLess(rendered.index('handle @preview_entry_get'),rendered.index('@internal path'))
        self.assertGreater(rendered.index('handle @preview_session'),rendered.index('handle @released'))
        with self.assertRaises(ValueError):render_preview_gateway(rendered,'00000000-0000-4000-8000-000000000001','https://game.example','10.0.0.1:8080',bootstrap)


class GamePreviewGatewayTests(unittest.TestCase):
    challenge_id='00000000-0000-4000-8000-000000000001'
    origin='https://game.example'
    upstream='127.0.0.1:8080'

    def setUp(self):
        self.original='''game.example {
 route {
  @internal path /internal /internal/* /__operator /__operator/*
  respond @internal "" 404
  @qa remote_ip 192.0.2.1/32
  handle @qa {
   reverse_proxy 127.0.0.1:8080 {
   }
  }
  @released expression `some_existing_window`
  handle @released {
   reverse_proxy 127.0.0.1:8080 {
   }
  }
  respond "KOTH_NOT_OPEN" 403
 }
}
score.example {
 respond "" 403
}'''
        bootstrap=(Path(__file__).parent/'koth-admin-preview.html').read_text(encoding='utf-8')
        self.v1=render_preview_gateway(self.original,self.challenge_id,self.origin,self.upstream,bootstrap)

    def render(self, **options):
        args=dict(original=self.v1,challenge_id=self.challenge_id,origin=self.origin,
                  upstream=self.upstream,game_slug='koth-jenga')
        args.update(options)
        return upgrade_game_preview(**args)

    def test_all_six_profiles_keep_existing_network_and_release_boundaries(self):
        for slug,(login,logout,card) in GAME_PROFILES.items():
            with self.subTest(game=slug):
                text=self.render(game_slug=slug)
                self.assertIn('path '+login,text)
                self.assertIn('/preview_sessions/game_proxy',text)
                self.assertEqual('/preview_sessions/game_authorize' in text,not card)
                self.assertIn('X-KOTH-Preview-URI {http.request.orig_uri}',text)
                self.assertIn('X-KOTH-Preview-Method {http.request.orig_method}',text)
                self.assertIn('header_up Cookie "(^|; *)__Host-msg_koth_[^=;]+=[^;]*" ""',text)
                self.assertNotIn('# msg-koth-admin-preview-v1 session begin',text)
                self.assertIn('@released expression `some_existing_window`',text)
                self.assertIn('respond @internal "" 404',text)
                self.assertIn('@qa remote_ip 192.0.2.1/32',text)
                self.assertTrue(text.endswith('score.example {\n respond "" 403\n}'))
                self.assertGreater(text.index('handle @preview_game'),text.index('handle @released'))

    def test_qa_candidate_is_isolated_and_retains_the_original_preview(self):
        text=self.render(qa_cookie_value='x'*43)
        self.assertIn('# msg-koth-admin-preview-v1 session begin',text)
        self.assertIn('{http.request.cookie.__Host-msg_koth_native_qa} == "'+'x'*43+'"',text)
        self.assertLess(text.index('handle @preview_game'),text.index('handle @preview_session'))

    def test_tab_indented_installed_markers_are_supported(self):
        self.assertIn('msg-koth-admin-preview-game-v2 begin',self.render(original=self.v1.replace('  # msg-koth-admin-preview-v1 session','\t# msg-koth-admin-preview-v1 session')))

    def test_login_assets_are_available_before_a_native_session_exists(self):
        self.assertIn('/common.js /images/* /login.css /login.js /style.css', self.render(game_slug='koth-swanopoly'))
        self.assertIn('/app.js /styles.css', self.render(game_slug='koth-dependency-hell'))
        self.assertNotIn('path /api/*', self.render())

    def test_pixel_public_round_and_reference_remain_read_only_in_preview(self):
        text=self.render(game_slug='koth-pixel-smuggler')
        public=text.split('@preview_game_public {',1)[1].split('}',1)[0]
        self.assertIn('method GET HEAD',public)
        self.assertIn('/api/v1/round /api/v1/leaderboard /api/v1/rounds/*/reference.png',public)
        self.assertIn('/api/v1/rounds/*/target.png',public)
        for path in ['/api/v1/me','/api/v1/submissions','/api/v1/rounds/* ']:
            self.assertNotIn(path,public)
        self.assertIn('uri /api/v1/koth/preview_sessions/game_authorize',text)

    def test_grand_login_page_and_public_rules_do_not_require_a_native_session(self):
        text=self.render(game_slug='koth-grand-tour-duel')
        public=text.split('@preview_game_public {',1)[1].split('}',1)[0]
        self.assertIn('method GET HEAD',public)
        for path in ['/team/login','/problem','/card-replay','/api/card/arena','/api/card/rating-rules']:
            self.assertIn(path,public)
        for path in ['/team/workspace','/api/team/session','/api/team/submissions','/api/card/games/*']:
            self.assertNotIn(path,public)

    def test_dependency_public_standings_and_exact_downloads_keep_write_authentication(self):
        text=self.render(game_slug='koth-dependency-hell')
        public=text.split('@preview_game_public {',1)[1].split('}',1)[0]
        self.assertIn('method GET HEAD',public)
        for path in ['/api/v1/leaderboard','/downloads/incident-analyzer','/downloads/dependency-hell-for-user.tar.gz']:
            self.assertIn(path,public)
        for path in ['/api/v1/me','/api/v1/submissions','/downloads/*']:
            self.assertNotIn(path,public)

    def test_repeated_or_malformed_upgrade_is_rejected(self):
        for original in [self.original,self.render(),self.v1+self.v1,
                         self.v1.replace('session begin','session end',1)]:
            with self.subTest(original=original[:20]),self.assertRaises(ValueError):self.render(original=original)

    def test_invalid_destinations_and_qa_guards_are_rejected(self):
        for options in [dict(origin='http://game.example'),dict(origin='https://game.example/path'),
                        dict(upstream='8.8.8.8:80'),dict(upstream='0.0.0.0:80'),
                        dict(upstream='127.0.0.1:65536'),dict(game_slug='unknown'),
                        dict(challenge_id='not-a-uuid'),dict(qa_cookie_value='short')]:
            with self.subTest(options=options),self.assertRaises(ValueError):self.render(**options)


if __name__=='__main__':unittest.main()
