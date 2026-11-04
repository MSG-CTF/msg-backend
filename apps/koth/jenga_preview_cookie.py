"""Validate the author's signed session while allowing its timestamp to refresh."""
import hashlib
import hmac
import json
import re

from django.views.decorators.debug import sensitive_variables
from itsdangerous import BadData, URLSafeTimedSerializer

from apps.common.jwt import hash_token
from apps.instances.runtime_secrets import RuntimeSecretUnavailable
from .environment import decode_settings
from .models import KothEnvironmentConfig

MAX_AGE = 12 * 60 * 60
SAFE_ID = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")


class InvalidJengaCookie(ValueError):
    pass


@sensitive_variables()
def credential_hash(challenge, raw, team_id):
    try:
        if not isinstance(raw, str) or not 1 <= len(raw) <= 4096 or not isinstance(team_id, str):
            raise ValueError()
        config = KothEnvironmentConfig.objects.select_related("applied_version").get(challenge=challenge)
        version = config.applied_version
        if (not version or version.challenge_id != challenge.pk
                or version.source_revision != challenge.source_revision):
            raise ValueError()
        containers = decode_settings(version)["containers"]
        entries = next(c["entries"] for c in containers if c["name"] == "jenga")
        secret = entries["SESSION_SECRET"]
        key, match_id, epoch = secret["value"], entries["KOTH_MATCH_ID"]["value"], entries["SESSION_EPOCH"]["value"]
        if (secret["kind"] != "secret" or not isinstance(key, str) or len(key) < 32
                or not isinstance(match_id, str) or not SAFE_ID.fullmatch(match_id)
                or not isinstance(epoch, str) or len(epoch) < 8):
            raise ValueError()
        serializer = URLSafeTimedSerializer(key, salt="cookie-session", serializer=json,
            signer_kwargs={"key_derivation": "hmac"})
        payload = serializer.loads(raw, max_age=MAX_AGE)
        if not isinstance(payload, dict):
            raise ValueError()
        generation = payload.get("generation")
        marker = hmac.new(key.encode(), f"{match_id}:{epoch}".encode(), hashlib.sha256).hexdigest()
        if (payload.get("team_id") != team_id or payload.get("match_id") != match_id
                or type(generation) is not int or not 1 <= generation < 2 ** 63
                or payload.get("_permanent") is not True or not isinstance(payload.get("marker"), str)
                or not hmac.compare_digest(payload["marker"], marker)):
            raise ValueError()
        identity = {"team_id": team_id, "match_id": match_id, "generation": generation, "marker": marker}
        return hash_token("jenga-session-v1:" + json.dumps(identity, sort_keys=True, separators=(",", ":")))
    except (BadData, ValueError, TypeError, KeyError, StopIteration, RuntimeSecretUnavailable,
            KothEnvironmentConfig.DoesNotExist):
        raise InvalidJengaCookie("Jenga session is invalid") from None
