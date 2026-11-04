import os
from pathlib import Path
import runpy
import secrets
from unittest import TestCase
from unittest.mock import patch

from django.core.exceptions import ImproperlyConfigured


class ServiceKeySettingsTests(TestCase):
    def load(self, **changes):
        environment = {
            "DJANGO_SECRET_KEY": secrets.token_urlsafe(48),
            "JWT_SECRET": secrets.token_urlsafe(48),
            "KOTH_TEAM_TOKEN_SECRET": secrets.token_urlsafe(48),
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

    def test_missing_empty_short_whitespace_and_control_keys_fail_in_all_modes(self):
        for debug in ["True", "False"]:
            for value in [
                None,
                "",
                "short-key",
                " " * 48,
                "a" * 40 + "\n",
                "a" * 20 + " " + "b" * 20,
            ]:
                with self.subTest(
                    debug=debug, shape=None if value is None else len(value)
                ):
                    with self.assertRaises(ImproperlyConfigured) as caught:
                        self.load(DJANGO_DEBUG=debug, KOTH_TEAM_TOKEN_SECRET=value)
                    self.assertIn("KOTH_TEAM_TOKEN_SECRET", str(caught.exception))
                    if value and value.strip():
                        self.assertNotIn(value, str(caught.exception))

    def test_equal_keys_are_rejected_without_echoing_key_material(self):
        key = secrets.token_urlsafe(48)
        for names in [
            ["JWT_SECRET", "KOTH_TEAM_TOKEN_SECRET"],
            ["DJANGO_SECRET_KEY", "KOTH_TEAM_TOKEN_SECRET"],
            ["JWT_SECRET", "DJANGO_SECRET_KEY"],
        ]:
            with self.subTest(names=names):
                with self.assertRaises(ImproperlyConfigured) as caught:
                    self.load(**dict.fromkeys(names, key))
                self.assertNotIn(key, str(caught.exception))

    def test_explicit_independent_keys_are_accepted(self):
        configured = self.load(DJANGO_DEBUG="False")
        self.assertFalse(configured["DEBUG"])
        self.assertEqual(
            len(
                {
                    configured[name]
                    for name in ["SECRET_KEY", "JWT_SECRET", "KOTH_TEAM_TOKEN_SECRET"]
                }
            ),
            3,
        )
