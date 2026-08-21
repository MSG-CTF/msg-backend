import json
import time
import urllib.error
import urllib.request
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.koth.models import KothChallenge, KothChallengeStatus
from apps.koth.services import apply_period_results

RETRY_DELAYS_SECONDS = [5, 15, 30]
REQUEST_TIMEOUT_SECONDS = 10


class Command(BaseCommand):
    """15분 구간 하나를 각 ACTIVE KOTH 문제 서버에 조회해서 점수를 반영한다.

    반복 실행 스케줄링(예: 15분마다 실행)은 이 저장소 밖(cron, k8s CronJob 등)에서 맡는다.
    문제 서버 주소(score_api_base_url)가 비어 있는 챌린지는 조용히 건너뛴다.
    """

    help = "ACTIVE 상태인 KOTH 문제 서버들의 /internal/koth/scores를 조회해 배점을 반영한다."

    def add_arguments(self, parser):
        parser.add_argument(
            "--period-id",
            help="조회할 15분 구간 시작 시각 (ISO-8601 UTC, 분은 00/15/30/45). 생략하면 현재 시각을 내림한 값을 쓴다.",
        )

    def handle(self, *args, **options):
        period_id = self._resolve_period_id(options.get("period_id"))
        scored_at = timezone.now()

        challenges = KothChallenge.objects.filter(
            status=KothChallengeStatus.ACTIVE, score_api_base_url__isnull=False
        ).exclude(score_api_base_url="")

        if not challenges.exists():
            self.stdout.write("조회할 ACTIVE KOTH 문제 서버가 없습니다 (score_api_base_url 미설정 포함).")
            return

        for challenge in challenges:
            self._poll_one(challenge, period_id, scored_at)

    def _resolve_period_id(self, raw):
        if raw:
            parsed = parse_datetime(raw)
            if parsed is None:
                raise ValueError(f"period_id 형식이 올바르지 않습니다: {raw}")
            return parsed
        now = timezone.now()
        floored_minute = (now.minute // 15) * 15
        return now.replace(minute=floored_minute, second=0, microsecond=0)

    def _poll_one(self, challenge, period_id, scored_at):
        period_str = period_id.strftime("%Y-%m-%dT%H:%M:%SZ")
        scored_at_str = scored_at.strftime("%Y-%m-%dT%H:%M:%SZ")
        url = (
            f"{challenge.score_api_base_url.rstrip('/')}/internal/koth/scores"
            f"?period_id={period_str}&scored_at={scored_at_str}"
        )

        last_error = None
        for attempt, delay in enumerate([0] + RETRY_DELAYS_SECONDS):
            if delay:
                time.sleep(delay)
            try:
                body = self._fetch(url, challenge.score_api_internal_token)
                self._handle_response(challenge, period_id, body)
                return
            except (urllib.error.URLError, TimeoutError, ValueError) as exc:
                last_error = exc
                self.stderr.write(
                    f"[{challenge.koth_challenge_id}] 시도 {attempt + 1} 실패: {exc}"
                )

        self.stderr.write(
            f"[{challenge.koth_challenge_id}] {period_str} 조회 최종 실패: {last_error}"
        )

    def _fetch(self, url, internal_token):
        request = urllib.request.Request(
            url, headers={"X-KOTH-Internal-Token": internal_token or ""}
        )
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            raw = response.read()
        return json.loads(raw)

    def _handle_response(self, challenge, period_id, body):
        code = body.get("code")
        if code != "SUCCESS":
            raise ValueError(f"문제 서버 오류 응답: {code} - {body.get('message')}")

        data = body.get("data")
        if data is None:
            self.stdout.write(f"[{challenge.koth_challenge_id}] 이 구간엔 결과가 없습니다.")
            return

        results = [
            {"team_id": row["team_id"], "period_rank": row["period_rank"]}
            for row in data.get("results", [])
        ]
        apply_period_results(challenge, period_id, results)
        self.stdout.write(
            f"[{challenge.koth_challenge_id}] {len(results)}팀 반영 완료 (period_id={period_id})."
        )
