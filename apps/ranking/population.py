"""Site-specific ranking reads, independent of the game's scoring population."""
import hashlib
import json

from django.conf import settings

from apps.koth.services import eligible_teams


def public_ranking_teams():
    teams = eligible_teams().filter(koth_division=settings.PUBLIC_RANKING_DIVISION)
    if settings.PUBLIC_RANKING_TEAM_IDS:
        teams = teams.filter(pk__in=settings.PUBLIC_RANKING_TEAM_IDS)
    return teams.exclude(pk__in=settings.PUBLIC_RANKING_EXCLUDED_TEAM_IDS)


def public_ranking_scope_key():
    scope = {
        "division": settings.PUBLIC_RANKING_DIVISION,
        "team_ids": sorted(str(value) for value in settings.PUBLIC_RANKING_TEAM_IDS),
        "excluded_team_ids": sorted(str(value) for value in settings.PUBLIC_RANKING_EXCLUDED_TEAM_IDS),
    }
    return hashlib.sha256(json.dumps(scope, sort_keys=True).encode()).hexdigest()[:32]
