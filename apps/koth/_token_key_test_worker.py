"""Fresh worker used only by isolated PostgreSQL process-restart tests."""

import json
import os
import sys


def main():
    configuration = json.load(sys.stdin)
    os.environ.update(configuration["environment"])
    os.environ["DJANGO_SETTINGS_MODULE"] = "config.settings"
    import django
    from django.conf import settings

    settings.DATABASES = {"default": configuration["database"]}
    settings.CACHES = {
        "default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}
    }
    settings.SECURE_SSL_REDIRECT = False
    settings.ALLOWED_HOSTS = ["testserver"]
    django.setup()
    from django.db import connections
    from rest_framework.test import APIClient

    client = APIClient()
    if configuration["action"] == "issue":
        client.credentials(HTTP_AUTHORIZATION="Bearer " + configuration["access_token"])
        response = client.get(
            "/api/v1/koth/team_token",
            {"koth_challenge_id": configuration["challenge_id"]},
        )
    else:
        response = client.post(
            "/internal/koth/team_tokens/verify",
            {
                "koth_challenge_id": configuration["challenge_id"],
                "team_token": configuration["team_token"],
            },
            format="json",
            HTTP_X_INTERNAL_TOKEN="inbound-secret",
        )
    print(
        json.dumps(
            {
                "status": response.status_code,
                "code": response.data["code"],
                "valid": (response.data["data"] or {}).get("valid"),
            }
        )
    )
    connections.close_all()


if __name__ == "__main__":
    main()
