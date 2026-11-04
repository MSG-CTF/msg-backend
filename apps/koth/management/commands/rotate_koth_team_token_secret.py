"""KOTH_TEAM_TOKEN_SECRET 교체 시 기존 팀 토큰 해시를 폐기한다.

- 비밀값을 바꾼 뒤 이 명령을 실행하면 koth_team_tokens 테이블의 해시가 모두 삭제돼
  이전에 발급된 토큰은 더 이상 검증을 통과하지 못한다.
- 선수는 POST /api/v1/koth/team_token 으로 새 토큰을 재발급 받으면 된다.
- 확인 플래그 없이는 돌지 않는다.
"""

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.koth.models import KothTeamToken


class Command(BaseCommand):
    help = "KOTH_TEAM_TOKEN_SECRET 교체 후 기존 팀 토큰 해시를 모두 폐기한다."

    def add_arguments(self, parser):
        parser.add_argument(
            "--yes-i-rotated-the-secret",
            action="store_true",
            help="비밀값을 실제로 교체했음을 확인한다. 이 플래그가 없으면 폐기하지 않는다.",
        )

    def handle(self, *args, **options):
        if not options["yes_i_rotated_the_secret"]:
            raise CommandError(
                "--yes-i-rotated-the-secret 플래그가 필요합니다. "
                "KOTH_TEAM_TOKEN_SECRET을 먼저 교체하고 이 명령을 실행하세요."
            )
        with transaction.atomic():
            count, _ = KothTeamToken.objects.all().delete()
        self.stdout.write(
            self.style.SUCCESS(
                f"폐기 완료: {count}개의 팀 토큰 해시가 삭제됐습니다. "
                "선수는 POST /api/v1/koth/team_token 으로 새 토큰을 재발급 받으세요."
            )
        )
