import os
from pathlib import Path
import runpy
import secrets
from unittest import TestCase
from unittest.mock import patch

from django.core.exceptions import ImproperlyConfigured


class KothTokenSecretSeparationTests(TestCase):
    def load(self, **changes):
        environment = {
            "DJANGO_SECRET_KEY": secrets.token_urlsafe(48),
            "JWT_SECRET": secrets.token_urlsafe(48),
            "KOTH_TEAM_TOKEN_SECRET": secrets.token_urlsafe(48),
            "DJANGO_DEBUG": "False",
        }
        environment.update(changes)
        environment = {
            name: value for name, value in environment.items() if value is not None
        }
        with (
            patch.dict(os.environ, environment, clear=True),
            patch("dotenv.load_dotenv"),
        ):
            return runpy.run_path(
                str(Path(__file__).resolve().parents[2] / "config/settings.py")
            )

    def test_production_without_koth_secret_is_refused(self):
        for value in [None, ""]:
            with (
                self.subTest(missing=value is None),
                self.assertRaises(ImproperlyConfigured),
            ):
                self.load(KOTH_TEAM_TOKEN_SECRET=value)

    def test_production_with_same_value_is_refused(self):
        key = secrets.token_urlsafe(48)
        with self.assertRaises(ImproperlyConfigured):
            self.load(JWT_SECRET=key, KOTH_TEAM_TOKEN_SECRET=key)

    def test_development_does_not_fall_back_to_the_jwt_key(self):
        with self.assertRaises(ImproperlyConfigured):
            self.load(DJANGO_DEBUG="True", KOTH_TEAM_TOKEN_SECRET=None)

    def test_django_key_cannot_be_reused_as_the_koth_key(self):
        key = secrets.token_urlsafe(48)
        with self.assertRaises(ImproperlyConfigured):
            self.load(DJANGO_SECRET_KEY=key, KOTH_TEAM_TOKEN_SECRET=key)
