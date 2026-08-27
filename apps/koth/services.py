import json
import os
import uuid
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import timedelta, timezone as dt_timezone
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.accounts.models import Team

from .models import KothScorePeriod, KothScorePeriodStatus, KothSolve

PERIOD_POINTS = {1: 40, 2: 25, 3: 15, 4: 12, 5: 8}


class ScoreFetchError(Exception):
    pass


def format_utc(value):
    return value.astimezone(dt_timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_period(value):
    parsed = parse_datetime(value) if isinstance(value, str) else value
    if parsed is None or timezone.is_naive(parsed):
        raise ScoreFetchError("period_id must be an ISO-8601 UTC datetime")
    parsed = parsed.astimezone(dt_timezone.utc).replace(second=0, microsecond=0)
    if parsed.minute not in (0, 15, 30, 45):
        raise ScoreFetchError("period_id must be aligned to a 15-minute boundary")
    return parsed


def _request_scores(challenge, period):
    if not challenge.score_api_url or not challenge.score_api_token_env:
        raise ScoreFetchError("score API URL or token environment variable is not configured")
    token = os.getenv(challenge.score_api_token_env)
    if not token:
        raise ScoreFetchError("score API token environment variable is empty")
    parsed_url = urllib.parse.urlsplit(challenge.score_api_url)
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.hostname:
        raise ScoreFetchError("score API URL must be an http or https URL")
    query = urllib.parse.urlencode({"period_id": format_utc(period), "scored_at": format_utc(period)})
    request = urllib.request.Request(
        f"{challenge.score_api_url}?{query}",
        headers={"X-KOTH-Internal-Token": token, "Accept": "application/json"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:  # nosec B310 - scheme and host validated above
            body = response.read().decode("utf-8")
            if response.status != 200:
                raise ScoreFetchError(f"score server returned HTTP {response.status}")
    except (urllib.error.URLError, TimeoutError, UnicodeDecodeError) as exc:
        raise ScoreFetchError(f"score server request failed: {exc}") from exc
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ScoreFetchError("score server returned invalid JSON") from exc
    if not isinstance(payload, dict) or payload.get("code") != "SUCCESS":
        raise ScoreFetchError("score server returned an unsuccessful payload")
    if payload.get("data") is not None and not isinstance(payload.get("data"), dict):
        raise ScoreFetchError("score server returned an invalid data payload")
    return payload


def _validated_results(challenge, period, payload):
    data = payload["data"]
    # 결과가 없는 구간은 명세상 data: null로 정상 응답할 수 있다.
    if data is None:
        return []
    if str(data.get("koth_challenge_id")) != str(challenge.koth_challenge_id):
        raise ScoreFetchError("score response challenge ID does not match")
    if parse_period(data.get("period_id")) != period:
        raise ScoreFetchError("score response period ID does not match")
    results = data.get("results")
    if not isinstance(results, list):
        raise ScoreFetchError("score response results must be a list")

    parsed, seen_teams = [], set()
    for row in results:
        if not isinstance(row, dict):
            raise ScoreFetchError("score result must be an object")
        try:
            team_id = uuid.UUID(str(row["team_id"]))
            rank = int(row["period_rank"])
            metric = Decimal(str(row["metric_score"]))
        except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
            raise ScoreFetchError("score result has invalid required fields") from exc
        if rank < 1 or not metric.is_finite() or team_id in seen_teams:
            raise ScoreFetchError("score result contains an invalid rank, metric, or duplicate team")
        seen_teams.add(team_id)
        parsed.append((team_id, rank))

    teams = Team.objects.in_bulk([team_id for team_id, _ in parsed], field_name="team_id")
    if len(teams) != len(parsed):
        raise ScoreFetchError("score response contains an unknown team")
    return [(teams[team_id], rank) for team_id, rank in parsed if not teams[team_id].is_banned]


def _awards(results):
    grouped = defaultdict(list)
    for team, rank in results:
        grouped[rank].append(team)
    awards = []
    for rank, teams in grouped.items():
        occupied_points = sum(PERIOD_POINTS.get(position, 0) for position in range(rank, rank + len(teams)))
        amount = Decimal(occupied_points // len(teams))
        awards.extend((team, amount) for team in teams if amount > 0)
    return awards


def poll_challenge_period(challenge, period):
    """하나의 문제/구간을 원자적으로 반영한다. 적용 완료 구간은 재반영하지 않는다."""
    period = parse_period(period)
    with transaction.atomic():
        record, _ = KothScorePeriod.objects.select_for_update().get_or_create(
            challenge=challenge, period_id=period
        )
        if record.status == KothScorePeriodStatus.APPLIED:
            return False
        record.attempts += 1
        record.save(update_fields=["attempts", "updated_at"])

    try:
        payload = _request_scores(challenge, period)
        results = _validated_results(challenge, period, payload)
    except ScoreFetchError as exc:
        KothScorePeriod.objects.filter(pk=record.pk).update(
            status=KothScorePeriodStatus.FAILED, last_error=str(exc), updated_at=timezone.now()
        )
        raise

    with transaction.atomic():
        record = KothScorePeriod.objects.select_for_update().get(pk=record.pk)
        if record.status == KothScorePeriodStatus.APPLIED:
            return False
        for team, amount in _awards(results):
            solve, _ = KothSolve.objects.select_for_update().get_or_create(
                team=team, challenge=challenge, defaults={"earned_score": Decimal("0")}
            )
            was_zero = solve.earned_score <= 0
            solve.earned_score += amount
            if was_zero and solve.earned_score > 0 and solve.solved_at is None:
                solve.solved_at = timezone.now()
            solve.save(update_fields=["earned_score", "solved_at", "updated_at"])
        record.status = KothScorePeriodStatus.APPLIED
        record.response_payload = payload
        record.last_error = ""
        record.applied_at = timezone.now()
        record.save(update_fields=["status", "response_payload", "last_error", "applied_at", "updated_at"])
    return True


def current_period(now=None):
    now = (now or timezone.now()).astimezone(dt_timezone.utc).replace(second=0, microsecond=0)
    return now - timedelta(minutes=now.minute % 15)
