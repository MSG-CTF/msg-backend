import importlib

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.test import TestCase


class KothTokenSecretSeparationTests(TestCase):
    """운영에서 KOTH_TEAM_TOKEN_SECRET이 JWT_SECRET과 다른 값으로 설정돼야 한다."""

    def _reload(self, env):
        import os

        saved = {k: os.environ.get(k) for k in env}
        for k, v in env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        try:
            from config import settings as settings_module

            importlib.reload(settings_module)
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            from config import settings as settings_module

            importlib.reload(settings_module)

    def test_production_without_koth_secret_is_refused(self):
        with self.assertRaises(ImproperlyConfigured):
            self._reload(
                {
                    "DJANGO_DEBUG": "False",
                    "SECRET_KEY": "x",
                    "JWT_SECRET": "aaaaaaaaaaaa",
                    "KOTH_TEAM_TOKEN_SECRET": "",
                }
            )

    def test_production_with_same_value_is_refused(self):
        with self.assertRaises(ImproperlyConfigured):
            self._reload(
                {
                    "DJANGO_DEBUG": "False",
                    "SECRET_KEY": "x",
                    "JWT_SECRET": "aaaaaaaaaaaa",
                    "KOTH_TEAM_TOKEN_SECRET": "aaaaaaaaaaaa",
                }
            )
