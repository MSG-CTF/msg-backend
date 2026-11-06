import json
import math
import time
import uuid

from django.core.management.base import BaseCommand, CommandError
from django.db import close_old_connections

from apps.instances.reconciliation import reconcile_instances_once
from apps.instances.services import SchedulerError
from apps.instances.write_recovery import recover_pending_writes_once


class Command(BaseCommand):
    help = "인스턴스의 Scheduler 관측 결과와 만료·종료 상태를 동기화합니다"

    def add_arguments(self, parser):
        parser.add_argument("--batch-size", type=int, default=100)
        parser.add_argument("--max-seconds", type=float, default=20)
        parser.add_argument("--interval", type=int, default=0, help="0이면 한 번만 실행합니다")
        parser.add_argument("--instance-id", type=uuid.UUID, action="append", default=[])

    def handle(self, *args, **options):
        if (
            not 1 <= options["batch_size"] <= 1000
            or not math.isfinite(options["max_seconds"])
            or options["max_seconds"] <= 0
            or options["interval"] < 0
        ):
            raise CommandError("batch-size는 1~1000, max-seconds는 유한한 양수, interval은 0 이상이어야 합니다")
        while True:
            close_old_connections()
            try:
                started = time.monotonic()
                write_recovery = (
                    recover_pending_writes_once(
                        batch_size=min(options["batch_size"], 10),
                        max_seconds=min(options["max_seconds"] / 3, 5),
                    )
                    if not options["instance_id"] else {"checked": 0}
                )
                summary = reconcile_instances_once(
                    batch_size=options["batch_size"],
                    max_seconds=max(0.001, options["max_seconds"] - (time.monotonic() - started)),
                    instance_ids=options["instance_id"],
                )
                if write_recovery["checked"]:
                    summary["write_recovery"] = write_recovery
            except SchedulerError as error:
                raise CommandError(f"종료 이력 동기화 설정 오류: {error.code}") from None
            self.stdout.write(json.dumps(summary))
            if not options["interval"]:
                if summary["error"]:
                    raise CommandError(f"종료 이력 동기화 오류 {summary['error']}건")
                return
            time.sleep(options["interval"])
