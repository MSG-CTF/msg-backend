import json
import uuid

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db.models import F

from apps.challenge.checker_dispatch import dispatch_submission
from apps.challenge.exe_submissions import ExeSubmissionError, close_exe_submission
from apps.challenge.models import ExeSubmission


class Command(BaseCommand):
    help = "Dispatch one specified Windows job or process a bounded batch."

    def add_arguments(self, parser):
        scope = parser.add_mutually_exclusive_group()
        scope.add_argument("--limit", type=int, default=10)
        scope.add_argument(
            "--submission-id",
            type=uuid.UUID,
            help="Dispatch/reconcile only this submission; do not run cleanup.",
        )

    def handle(self, *args, **options):
        if not settings.EXE_DISPATCH_ENABLED or not settings.EXE_SUBMISSIONS_ENABLED:
            self.stdout.write("Windows execution is disabled")
            return
        if options["submission_id"] is not None:
            row = ExeSubmission.objects.filter(pk=options["submission_id"]).first()
            if row is None:
                raise CommandError("SUBMISSION_NOT_FOUND")
            if row.config_snapshot.get("assignment_mode") != "broker_pool":
                raise CommandError("LEGACY_EXECUTION_REQUIRES_REVIEW")
            try:
                row = dispatch_submission(row.pk)
            except ExeSubmissionError as error:
                raise CommandError(error.code) from None
            self.stdout.write(
                json.dumps({
                    "submission_id": str(row.pk),
                    "status": row.status,
                    "dispatch_state": row.dispatch_state,
                    "checker_job_id": row.checker_job_id,
                    "last_error_code": row.last_error_code,
                })
            )
            return
        limit = max(1, min(options["limit"], 100))
        rows = ExeSubmission.objects.filter(
            config_snapshot__assignment_mode="broker_pool"
        ).exclude(config_snapshot__has_key="private_qa")
        ids = (
            rows.filter(status="ASSIGNED", finished_at__isnull=True)
            .order_by(F("dispatch_started_at").asc(nulls_first=True), "created_at", "submission_id")
            .values_list("pk", flat=True)[:limit]
        )
        for submission_id in ids:
            try:
                dispatch_submission(submission_id)
            except ExeSubmissionError as error:
                self.stderr.write(error.code)
        ids = (
            rows.filter(
                lease_released_at__isnull=False,
                cleanup_complete=True,
                cleanup_confirmed_at__isnull=False,
            )
            .exclude(payload_key="")
            .values_list("pk", flat=True)[:limit]
        )
        for submission_id in ids:
            close_exe_submission(submission_id)
        ids = (
            rows.filter(
                cleanup_complete=True,
                cleanup_confirmed_at__isnull=False,
                finished_at__isnull=False,
                lease_released_at__isnull=True,
            )
            .exclude(attempt_state="RESERVED")
            .values_list("pk", flat=True)[:limit]
        )
        for submission_id in ids:
            try:
                close_exe_submission(submission_id)
            except ExeSubmissionError as error:
                self.stderr.write(error.code)
