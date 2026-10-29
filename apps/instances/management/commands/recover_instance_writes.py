import json
import uuid

from django.core.management.base import BaseCommand, CommandError

from apps.instances.models import InstanceWriteOperation
from apps.instances.write_recovery import recover_write


class Command(BaseCommand):
    help = "원격 생성·reset을 재실행하지 않고 실패한 인스턴스 DB 저장을 복구합니다"

    def add_arguments(self, parser):
        parser.add_argument("--operation-id", type=uuid.UUID)
        parser.add_argument("--limit", type=int, default=20)
        parser.add_argument(
            "--show", action="store_true", help="미완료 작업만 조회합니다"
        )

    def handle(self, *args, **options):
        if not 1 <= options["limit"] <= 100:
            raise CommandError("limit은 1~100이어야 합니다")
        rows = InstanceWriteOperation.objects.order_by("created_at", "operation_id")
        if options["operation_id"]:
            rows = rows.filter(pk=options["operation_id"])
            if not rows.exists():
                raise CommandError("작업을 찾을 수 없습니다")
        else:
            rows = (
                rows.filter(
                    status__in=["STARTED", "RECOVERY_PENDING", "REVIEW_REQUIRED"]
                )
                if options["show"]
                else rows.filter(status="RECOVERY_PENDING")
            )
        incomplete = 0
        for row in rows[: options["limit"]]:
            status = (
                row.status
                if options["show"]
                else recover_write(
                    row.pk,
                    review=options["operation_id"] is not None,
                )
            )
            row.refresh_from_db()
            self.stdout.write(
                json.dumps(
                    {
                        "operation_id": str(row.pk),
                        "action": row.action,
                        "status": status,
                        "user_id": str(row.user_id),
                        "team_id": str(row.team_id),
                        "challenge_id": str(row.challenge_id),
                        "release_id": str(row.release_id) if row.release_id else None,
                        "source_instance_id": (
                            str(row.source_instance_id)
                            if row.source_instance_id
                            else None
                        ),
                        "remote_instance_id": (
                            str(row.remote_instance_id)
                            if row.remote_instance_id
                            else None
                        ),
                        "attempts": row.attempts,
                        "last_error_code": row.last_error_code,
                        "updated_at": row.updated_at.isoformat(),
                    }
                )
            )
            incomplete += status != InstanceWriteOperation.Status.COMPLETED
        if incomplete and not options["show"]:
            raise CommandError(
                f"미완료 복구 작업 {incomplete}건: 저장된 상태를 확인해주세요"
            )
