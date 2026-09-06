import time

from django.conf import settings
from django.core.management.base import BaseCommand

from apps.instances.poller import poll_once


class Command(BaseCommand):
    help = (
        "공급망 publish bundle을 수집해 새 릴리스를 자동 등록한다. "
        "기본은 1회 실행이라 cron에 걸기 좋고, --interval을 주면 주기 실행한다. "
        "전환은 하지 않으므로 배포 버전 선택은 관리자 몫으로 남는다."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--interval",
            type=int,
            default=0,
            help="초 단위 반복 주기. 0이면 1회만 실행",
        )

    def handle(self, *args, **options):
        token = settings.RELEASE_POLL_GITHUB_TOKEN
        if not token:
            self.stderr.write(
                "RELEASE_POLL_GITHUB_TOKEN이 비어 있습니다. "
                "Actions artifact 다운로드에는 토큰이 필요합니다."
            )

        interval = options["interval"]
        while True:
            summary = poll_once(token=token)
            self.stdout.write(
                "poll 완료: 등록 {registered}, 중복 {duplicate}, "
                "매핑 실패 {unmatched}, 형식 오류 {invalid}, 통신 오류 {error}".format(**summary)
            )
            if interval <= 0:
                break
            time.sleep(interval)
