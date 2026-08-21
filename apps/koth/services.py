import uuid
from datetime import timedelta

from django.core.cache import cache
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from apps.accounts.models import Team
from apps.common.jwt import hash_token

from .exceptions import (
    ClubNotFound,
    InvalidClubId,
    InvalidKothChallengeId,
    KothChallengeIdRequired,
    KothChallengeNotFound,
    TooManyAttempts,
)
from .models import (
    KothChallenge,
    KothChallengeStatus,
    KothClub,
    KothScoringPeriod,
    KothSolve,
    KothTeamToken,
)

VERIFY_FAILURE_LIMIT = 3
VERIFY_LOCK_SECONDS = 30

# 15분 구간 배점표 (koth-template/prob/for_organizer/admin.md "배점표" 확정, 2026-08-16).
# 등수 미만으로 순위가 매겨진 팀이 없으면 남는 자리 점수는 아무에게도 지급하지 않는다.
KOTH_PERIOD_POINTS = {1: 40, 2: 25, 3: 15, 4: 12, 5: 8}


def _period_table_value(position):
    return KOTH_PERIOD_POINTS.get(position, 0)


def build_challenge_entry(challenge):
    top_solve = (
        KothSolve.objects.select_related("team")
        .filter(challenge=challenge)
        .order_by("-earned_score", "solved_at")
        .first()
    )
    return {
        "koth_challenge_id": str(challenge.koth_challenge_id),
        "title": challenge.title,
        "category": challenge.category,
        "status": challenge.status,
        "open_group": challenge.open_group,
        "current_owner_team_id": str(top_solve.team_id) if top_solve else None,
        "current_owner_team_name": top_solve.team.team_name if top_solve else None,
        "current_score": top_solve.earned_score if top_solve else 0,
        "opened_at": challenge.opened_at,
        "closed_at": challenge.closed_at,
    }


def build_club_entry(club, challenges):
    return {
        "club_id": str(club.club_id),
        "name": club.name,
        "challenges": [build_challenge_entry(challenge) for challenge in challenges],
    }


def get_clubs_list():
    clubs = list(KothClub.objects.order_by("club_id"))
    challenges = list(
        KothChallenge.objects.select_related("club").order_by("club_id", "koth_challenge_id")
    )
    challenges_by_club = {}
    for challenge in challenges:
        challenges_by_club.setdefault(challenge.club_id, []).append(challenge)

    return {
        "clubs": [build_club_entry(club, challenges_by_club.get(club.club_id, [])) for club in clubs],
        "total_count": len(challenges),
        "active_count": sum(1 for c in challenges if c.status == KothChallengeStatus.ACTIVE),
    }


def get_club_detail(club_id_raw):
    try:
        club_id = uuid.UUID(str(club_id_raw))
    except (ValueError, TypeError):
        raise InvalidClubId()

    club = KothClub.objects.filter(pk=club_id).first()
    if club is None:
        raise ClubNotFound()

    challenges = list(club.challenges.order_by("koth_challenge_id"))
    return build_club_entry(club, challenges)


def get_koth_me(team):
    challenges = list(
        KothChallenge.objects.select_related("club").order_by("club_id", "koth_challenge_id")
    )
    solves = {solve.challenge_id: solve for solve in KothSolve.objects.filter(team=team)}

    items = []
    total_score = 0
    active_count = 0
    for challenge in challenges:
        solve = solves.get(challenge.koth_challenge_id)
        earned_score = solve.earned_score if solve else 0
        total_score += earned_score
        if challenge.status == KothChallengeStatus.ACTIVE:
            active_count += 1

        rank = None
        if solve is not None:
            rank = (
                KothSolve.objects.filter(
                    challenge=challenge, earned_score__gt=solve.earned_score
                ).count()
                + 1
            )

        items.append(
            {
                "koth_challenge_id": str(challenge.koth_challenge_id),
                "club_id": str(challenge.club_id),
                "title": challenge.title,
                "category": challenge.category,
                "status": challenge.status,
                "earned_score": earned_score,
                "rank": rank,
                "solved_at": solve.solved_at if solve else None,
                "opened_at": challenge.opened_at,
                "closed_at": challenge.closed_at,
            }
        )

    return {
        "team_id": str(team.team_id),
        "team_name": team.team_name,
        "total_koth_score": total_score,
        "challenges": items,
        "total_count": len(items),
        "active_count": active_count,
    }


def get_leaderboard(koth_challenge_id_raw):
    if not koth_challenge_id_raw:
        raise KothChallengeIdRequired()
    try:
        koth_challenge_id = uuid.UUID(str(koth_challenge_id_raw))
    except ValueError:
        raise InvalidKothChallengeId()

    challenge = KothChallenge.objects.filter(pk=koth_challenge_id).first()
    if challenge is None:
        raise KothChallengeNotFound()

    solves = (
        KothSolve.objects.select_related("team")
        .filter(challenge=challenge)
        .order_by("-earned_score", "solved_at", "team_id")
    )
    leaderboard = [
        {
            "rank": index + 1,
            "team_id": str(solve.team_id),
            "team_name": solve.team.team_name,
            "earned_score": solve.earned_score,
            "solved_at": solve.solved_at,
        }
        for index, solve in enumerate(solves)
    ]

    return {
        "koth_challenge_id": str(challenge.koth_challenge_id),
        "title": challenge.title,
        "status": challenge.status,
        "leaderboard": leaderboard,
        "total_count": len(leaderboard),
        "updated_at": timezone.now(),
    }


def get_or_create_team_token(team):
    token, _ = KothTeamToken.objects.get_or_create(team=team)
    return token


# ---------------------------------------------------------------------------
# team_tokens/verify — 브루트포스 방어 (koth-template admin.md "검증 제한 정책", 2026-08-16)
# ---------------------------------------------------------------------------


def _verify_attempt_key(koth_challenge_id, team_token):
    return f"koth_verify_attempt:{koth_challenge_id}:{hash_token(team_token)}"


def _check_verify_lock(koth_challenge_id, team_token):
    state = cache.get(_verify_attempt_key(koth_challenge_id, team_token))
    if not state or not state.get("locked_until"):
        return
    remaining = (state["locked_until"] - timezone.now()).total_seconds()
    if remaining > 0:
        raise TooManyAttempts(retry_after_seconds=max(1, int(remaining) + 1))


def _register_verify_success(koth_challenge_id, team_token):
    cache.delete(_verify_attempt_key(koth_challenge_id, team_token))


def _register_verify_failure(koth_challenge_id, team_token):
    """3번째 연속 실패가 발생한 이 요청 자체가 429를 받는다 (admin.md 검증 제한 정책)."""
    key = _verify_attempt_key(koth_challenge_id, team_token)
    state = cache.get(key) or {"failures": 0, "locked_until": None}
    state["failures"] += 1
    if state["failures"] >= VERIFY_FAILURE_LIMIT:
        state["locked_until"] = timezone.now() + timedelta(seconds=VERIFY_LOCK_SECONDS)
        cache.set(key, state, VERIFY_LOCK_SECONDS)
        raise TooManyAttempts(retry_after_seconds=VERIFY_LOCK_SECONDS)
    cache.set(key, state, VERIFY_LOCK_SECONDS * 10)


def verify_team_token(koth_challenge_id_raw, team_token):
    try:
        koth_challenge_id = uuid.UUID(str(koth_challenge_id_raw))
    except (ValueError, TypeError):
        koth_challenge_id = None

    invalid_result = {
        "valid": False,
        "team_id": None,
        "team_name": None,
        "koth_challenge_id": str(koth_challenge_id) if koth_challenge_id else None,
    }
    if koth_challenge_id is None or not team_token:
        return invalid_result

    _check_verify_lock(koth_challenge_id, team_token)

    if not KothChallenge.objects.filter(pk=koth_challenge_id).exists():
        _register_verify_failure(koth_challenge_id, team_token)
        return invalid_result

    token_row = KothTeamToken.objects.select_related("team").filter(token=team_token).first()
    if token_row is None or token_row.team.is_banned:
        _register_verify_failure(koth_challenge_id, team_token)
        return invalid_result

    _register_verify_success(koth_challenge_id, team_token)
    return {
        "valid": True,
        "team_id": str(token_row.team_id),
        "team_name": token_row.team.team_name,
        "koth_challenge_id": str(koth_challenge_id),
    }


def get_internal_teams():
    teams = Team.objects.filter(is_banned=False).order_by("team_name")
    items = [{"team_id": str(team.team_id), "team_name": team.team_name} for team in teams]
    return {"teams": items, "total_count": len(items)}


# ---------------------------------------------------------------------------
# /internal/koth/scores — 배점 엔진 (admin.md "배점표", 2026-08-16 확정)
# ---------------------------------------------------------------------------


def compute_period_awards(results):
    """results: [{"team_id": UUID, "period_rank": int}, ...] (실격/인증실패 팀은 이미 제외됨).

    같은 period_rank를 공유하는 팀들은 그 팀들이 차지한 연속된 등수 배점을 합산해
    균등 분배하고 내림한다. 나머지(재분배하지 않는 몫)는 그냥 지급하지 않는다.
    반환: {team_id: awarded_score}
    """
    by_rank = {}
    for row in results:
        by_rank.setdefault(row["period_rank"], []).append(row["team_id"])

    awards = {}
    for rank, team_ids in by_rank.items():
        group_size = len(team_ids)
        slot_sum = sum(_period_table_value(rank + offset) for offset in range(group_size))
        per_team = slot_sum // group_size
        for team_id in team_ids:
            awards[team_id] = per_team

    return awards


def apply_period_results(challenge, period_id, results):
    """문제 서버의 /internal/koth/scores 응답(results[])을 받아 팀별 KOTH 누적 점수에 반영한다.

    같은 (challenge, period_id) 조합은 한 번만 반영한다 (멱등).
    """
    with transaction.atomic():
        period, created = KothScoringPeriod.objects.get_or_create(
            challenge=challenge,
            period_id=period_id,
            defaults={
                "raw_response": [
                    {"team_id": str(row["team_id"]), "period_rank": row["period_rank"]}
                    for row in results
                ],
            },
        )
        if not created:
            return period

        awards = compute_period_awards(results)
        for team_id, awarded_score in awards.items():
            if awarded_score <= 0:
                continue
            team = Team.objects.filter(pk=team_id).first()
            if team is None:
                continue
            solve, _ = KothSolve.objects.get_or_create(
                team=team, challenge=challenge, defaults={"earned_score": 0}
            )
            KothSolve.objects.filter(pk=solve.pk).update(
                earned_score=F("earned_score") + awarded_score
            )

    return period
