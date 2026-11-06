"""Exercise both real HTTP applications and their PostgreSQL/Redis/SQLite stores.

Run this suite explicitly with the test-only requirements installed. Missing or
different upstream source fails the check instead of skipping integration.
"""
from contextlib import closing
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import uuid

from django.core.cache import cache
from django.test import LiveServerTestCase
from django.utils import timezone
import requests
from werkzeug.serving import WSGIRequestHandler, make_server

from apps.accounts.models import Team
from apps.common.jwt import hash_token
from apps.koth._key_test_support import initialize_token_key
from apps.koth.models import KothChallenge, KothClub, KothTeamToken, KothTokenKeyState
from apps.koth.tokens import build_team_token


def load_author():
    contract = json.loads(Path(__file__).with_name('upstream.json').read_text())
    source = Path(os.environ['JENGA_SOURCE_ROOT']).resolve()
    for entry in contract['files']:
        path = source / entry['path']
        canonical = path.read_bytes().replace(b'\r\n', b'\n')
        if path.is_symlink() or hashlib.sha256(canonical).hexdigest() != entry['sha256']:
            raise RuntimeError('Jenga source differs from the reviewed contract')
    sys.path.insert(0, str(source))
    spec = importlib.util.spec_from_file_location('jenga_contract_author', source / 'app.py')
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class QuietHandler(WSGIRequestHandler):
    def log(self, kind, message, *args):
        pass


class GameClient:
    def __init__(self, origin):
        self.origin = origin
        self.session = requests.Session()
        self.session.trust_env = False

    def get(self, path, **kwargs):
        return self.session.get(self.origin + path, timeout=(1, 5), **kwargs)

    def post(self, path, **kwargs):
        return self.session.post(self.origin + path, timeout=(1, 5), **kwargs)

    def cookie(self):
        return self.session.cookies.get('jenga_session')


class JengaPlatformSessionContractTests(LiveServerTestCase):
    @classmethod
    def setUpClass(cls):
        cls.author = load_author()
        super().setUpClass()

    def setUp(self):
        cache.clear()
        initialize_token_key()
        self.team = Team.objects.create(team_name='Jenga contract one')
        self.other_team = Team.objects.create(team_name='Jenga contract two')
        self.internal = 'jenga-contract-internal-' + 'i' * 40
        self.challenge = KothChallenge.objects.create(
            club=KothClub.objects.create(name='Jenga session contract'), title='Jenga',
            repository_slug='koth-jenga', status='ACTIVE', open_group=1,
            opened_at=timezone.now(), inbound_internal_token_hash=hash_token(self.internal))
        self.tokens = {}
        for team in [self.team, self.other_team]:
            token = build_team_token(team.pk, self.challenge.pk)
            KothTeamToken.objects.create(team=team, challenge=self.challenge,
                                         token_hash=hash_token(token), key_version=1)
            self.tokens[team.pk] = token
        self.now = 1_800_000_001.0
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.game = self.application()

    def application(self, **changes):
        app = self.author.create_app({
            'TESTING': True, 'DATABASE': str(Path(self.directory.name) / 'game.db'),
            'SESSION_SECRET': 's' * 64, 'SESSION_EPOCH': 'contract-20261106',
            'KOTH_JENGA_TOKEN': 'k' * 64, 'GAME_SECRET': 'g' * 64,
            'KOTH_MATCH_ID': 'platform-session-contract',
            'KOTH_CHALLENGE_ID': str(self.challenge.pk),
            'PLATFORM_URL': self.live_server_url, 'INTERNAL_TOKEN': self.internal,
            'ALLOW_DEMO_AUTH': False, 'SESSION_COOKIE_SECURE': False,
            'START_TICK_WORKER': False, 'NOW_FN': lambda: self.now, **changes})
        server = make_server('127.0.0.1', 0, app, threaded=True, request_handler=QuietHandler)
        thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=.05), daemon=True)
        thread.start()

        def stop():
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()
        self.addCleanup(stop)
        return {'app': app, 'origin': f'http://127.0.0.1:{server.server_port}'}

    def game_client(self, game=None, cookie=None):
        client = GameClient((game or self.game)['origin'])
        self.addCleanup(client.session.close)
        if cookie:
            client.session.cookies.set('jenga_session', cookie, domain='127.0.0.1', path='/')
        return client

    def login(self, team=None, game=None):
        team = team or self.team
        client = self.game_client(game)
        response = client.post('/auth', json={'team_token': self.tokens[team.pk]})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['data']['team_id'], str(team.pk))
        self.assertTrue(client.cookie())
        return client

    def game_rows(self):
        store = self.game['app'].extensions['jenga_store']
        queries = {
            'jenga_matches': 'SELECT * FROM jenga_matches ORDER BY rowid',
            'jenga_teams': 'SELECT * FROM jenga_teams ORDER BY rowid',
            'jenga_strategies': 'SELECT * FROM jenga_strategies ORDER BY rowid',
            'jenga_ticks': 'SELECT * FROM jenga_ticks ORDER BY rowid',
            'jenga_periods': 'SELECT * FROM jenga_periods ORDER BY rowid',
            'jenga_mutations': 'SELECT * FROM jenga_mutations ORDER BY rowid',
        }
        with closing(store.connect()) as connection:
            return {name: [tuple(row) for row in connection.execute(query)]
                    for name, query in queries.items()}

    def test_normal_login_uses_current_cookie_namespace_and_server_handle(self):
        client = self.login()
        self.assertIsNone(client.session.cookies.get('session'))
        payload = self.game['app'].session_interface.get_signing_serializer(self.game['app']).loads(client.cookie())
        self.assertEqual(payload['match_id'], 'platform-session-contract')
        self.assertEqual(payload['generation'], 1)
        self.assertRegex(payload['sid'], r'^[A-Za-z0-9_-]{43}$')
        self.assertNotIn(self.tokens[self.team.pk], json.dumps(payload))
        self.assertEqual(client.get('/state').status_code, 200)

    def test_protected_reads_do_not_extend_the_cookie(self):
        client = self.login()
        cookie = client.cookie()
        for _ in range(2):
            response = client.get('/state')
            self.assertEqual(response.status_code, 200)
            self.assertNotIn('Set-Cookie', response.headers)
            self.assertEqual(client.cookie(), cookie)

    def test_ban_discards_a_warmed_cookie_and_unban_requires_new_login(self):
        client = self.login()
        cookie = client.cookie()
        self.assertEqual(client.get('/state').status_code, 200)
        Team.objects.filter(pk=self.team.pk).update(is_banned=True)
        self.assertEqual(client.get('/state').status_code, 401)
        Team.objects.filter(pk=self.team.pk).update(is_banned=False)
        self.assertEqual(self.game_client(cookie=cookie).get('/state').status_code, 401)
        self.assertEqual(self.login().get('/state').status_code, 200)

    def test_token_deletion_cannot_revive_a_cookie_after_the_row_is_restored(self):
        client = self.login()
        cookie = client.cookie()
        KothTeamToken.objects.filter(team=self.team, challenge=self.challenge).delete()
        self.assertEqual(client.get('/state').status_code, 401)
        KothTeamToken.objects.create(team=self.team, challenge=self.challenge,
            token_hash=hash_token(self.tokens[self.team.pk]), key_version=1)
        self.assertEqual(self.game_client(cookie=cookie).get('/state').status_code, 401)
        self.assertEqual(self.login().get('/state').status_code, 200)

    def test_token_replacement_cannot_revive_a_discarded_session(self):
        client = self.login()
        cookie = client.cookie()
        row = KothTeamToken.objects.get(team=self.team, challenge=self.challenge)
        row.token_hash = 'e' * 64
        row.save(update_fields=['token_hash'])
        self.assertEqual(client.get('/state').status_code, 401)
        row.token_hash = hash_token(self.tokens[self.team.pk])
        row.save(update_fields=['token_hash'])
        self.assertEqual(self.game_client(cookie=cookie).get('/state').status_code, 401)
        self.assertEqual(self.login().get('/state').status_code, 200)

    def test_each_protected_mutation_checks_the_current_ban_before_game_changes(self):
        mutations = [('/strategy', {'strategy': [{'width': 4, 'offset': 0}]}),
                     ('/strategy/stop', {}), ('/place', {'width': 4, 'offset': 0})]
        for route, payload in mutations:
            with self.subTest(route=route):
                client = self.login()
                self.assertEqual(client.get('/state').status_code, 200)
                before = self.game_rows()
                Team.objects.filter(pk=self.team.pk).update(is_banned=True)
                response = client.post(route, json=payload, headers={'Idempotency-Key': str(uuid.uuid4())})
                self.assertEqual(response.status_code, 401, response.text)
                self.assertEqual(self.game_rows(), before)
                Team.objects.filter(pk=self.team.pk).update(is_banned=False)

    def test_banning_one_team_does_not_discard_another_teams_cookie(self):
        banned, other = self.login(), self.login(self.other_team)
        Team.objects.filter(pk=self.team.pk).update(is_banned=True)
        self.assertEqual(banned.get('/state').status_code, 401)
        self.assertEqual(other.get('/state').status_code, 200)

    def test_actual_platform_authority_failure_blocks_without_revoking_and_recovers(self):
        client = self.login()
        cookie, before = client.cookie(), self.game_rows()
        original = KothTokenKeyState.objects.get(pk=1).fingerprint
        KothTokenKeyState.objects.filter(pk=1).update(fingerprint='0' * 64)
        response = client.post('/strategy/stop', json={}, headers={'Idempotency-Key': str(uuid.uuid4())})
        self.assertEqual(response.status_code, 502)
        self.assertNotIn('Set-Cookie', response.headers)
        self.assertEqual(client.cookie(), cookie)
        self.assertEqual(self.game_rows(), before)
        KothTokenKeyState.objects.filter(pk=1).update(fingerprint=original)
        self.assertEqual(client.get('/state').status_code, 200)

    def test_actual_internal_credential_failure_recovers_without_cookie_renewal(self):
        client = self.login()
        cookie = client.cookie()
        self.game['app'].config['INTERNAL_TOKEN'] = 'wrong-internal-credential'
        response = client.get('/state')
        self.assertEqual(response.status_code, 502)
        self.assertNotIn('Set-Cookie', response.headers)
        self.assertEqual(client.cookie(), cookie)
        self.game['app'].config['INTERNAL_TOKEN'] = self.internal
        self.assertEqual(client.get('/state').status_code, 200)

    def test_restart_preserves_valid_session_and_observes_later_revocation(self):
        cookie = self.login().cookie()
        restarted = self.application()
        client = self.game_client(restarted, cookie)
        self.assertEqual(client.get('/state').status_code, 200)
        Team.objects.filter(pk=self.team.pk).update(is_banned=True)
        self.assertEqual(client.get('/state').status_code, 401)
        Team.objects.filter(pk=self.team.pk).update(is_banned=False)
        self.assertEqual(self.game_client(cookie=cookie).get('/state').status_code, 401)

    def test_cookie_cannot_cross_match_namespace(self):
        cookie = self.login().cookie()
        other = self.application(KOTH_MATCH_ID='another-contract-match')
        self.assertEqual(self.game_client(other, cookie).get('/state').status_code, 401)
        self.assertEqual(self.game_client(cookie=cookie).get('/state').status_code, 200)

    def test_generation_reset_requires_a_new_session(self):
        client = self.login()
        cookie = client.cookie()
        response = client.post('/internal/koth/matches/reset',
            json={'match_id': 'platform-session-contract', 'confirm': 'reset:platform-session-contract', 'expected_generation': 1},
            headers={'X-KOTH-Internal-Token': 'k' * 64, 'Idempotency-Key': str(uuid.uuid4())})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.game_client(cookie=cookie).get('/state').status_code, 401)
        self.assertEqual(self.login().get('/state').status_code, 200)

    def test_epoch_rotation_retires_old_workers_and_preserves_game_rows(self):
        client = self.login()
        cookie, before = client.cookie(), self.game_rows()
        rotated = self.application(SESSION_EPOCH='contract-new-epoch-20261106')
        self.assertEqual(self.game_client(rotated, cookie).get('/state').status_code, 401)
        self.assertEqual(client.get('/state').status_code, 401)
        response = self.game_client().post('/auth', json={'team_token': self.tokens[self.team.pk]})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.game_rows(), before)
        self.assertEqual(self.login(game=rotated).get('/state').status_code, 200)

    def test_old_stateless_signed_cookie_cannot_replace_the_server_handle(self):
        client = self.login()
        signer = self.game['app'].session_interface.get_signing_serializer(self.game['app'])
        payload = signer.loads(client.cookie())
        payload.pop('sid')
        self.assertEqual(self.game_client(cookie=signer.dumps(payload)).get('/state').status_code, 401)

    def test_fixed_session_expiry_cannot_be_undone_by_clock_restoration(self):
        client = self.login()
        cookie = client.cookie()
        self.now += 12 * 3600
        self.assertEqual(client.get('/state').status_code, 401)
        self.now -= 12 * 3600
        self.assertEqual(self.game_client(cookie=cookie).get('/state').status_code, 401)

    def test_token_for_another_challenge_cannot_authenticate(self):
        internal = 'jenga-other-contract-internal-' + 'o' * 40
        other = KothChallenge.objects.create(club=KothClub.objects.create(name='Other Jenga contract'),
            title='Another challenge', status='ACTIVE', open_group=1,
            inbound_internal_token_hash=hash_token(internal))
        game = self.application(KOTH_CHALLENGE_ID=str(other.pk), KOTH_MATCH_ID='another-challenge',
                                INTERNAL_TOKEN=internal)
        response = self.game_client(game).post('/auth', json={'team_token': self.tokens[self.team.pk]})
        self.assertEqual(response.status_code, 401)

    def test_actual_platform_contract_rejects_camel_case_and_returns_boolean_uuid_fields(self):
        session = requests.Session()
        session.trust_env = False
        self.addCleanup(session.close)
        url = self.live_server_url + '/internal/koth/team_tokens/verify'
        headers = {'X-Internal-Token': self.internal}
        response = session.post(url, headers=headers, json={'kothChallengeId': str(self.challenge.pk),
            'teamToken': self.tokens[self.team.pk]}, timeout=(1, 5))
        self.assertEqual(response.status_code, 400)
        response = session.post(url, headers=headers, json={'koth_challenge_id': str(self.challenge.pk),
            'team_token': self.tokens[self.team.pk]}, timeout=(1, 5))
        self.assertEqual(response.status_code, 200)
        data = response.json()['data']
        self.assertIs(data['valid'], True)
        self.assertEqual(data['team_id'], str(self.team.pk))
        self.assertEqual(data['koth_challenge_id'], str(self.challenge.pk))
        self.assertEqual(set(data), {'valid', 'team_id', 'team_name', 'koth_division', 'koth_challenge_id'})

    def test_tampered_server_token_ciphertext_is_rejected(self):
        client = self.login()
        cookie = client.cookie()
        store = self.game['app'].extensions['jenga_store']
        with closing(store.connect()) as connection:
            connection.execute("UPDATE jenga_auth_sessions SET encrypted_team_token='tampered'")
        self.assertEqual(client.get('/state').status_code, 401)
        self.assertEqual(self.game_client(cookie=cookie).get('/state').status_code, 401)
        self.assertEqual(self.login().get('/state').status_code, 200)
