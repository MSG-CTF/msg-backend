from django.conf import settings
from django.core.management.base import BaseCommand

from apps.challenge.checker_dispatch import dispatch_submission
from apps.challenge.exe_submissions import ExeSubmissionError, close_exe_submission
from apps.challenge.models import ExeSubmission


class Command(BaseCommand):
    help = "Process one bounded batch of persisted Windows jobs; execution is disabled by default."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=10)

    def handle(self, *args, **options):
        if not settings.EXE_DISPATCH_ENABLED or not settings.EXE_SUBMISSIONS_ENABLED:
            self.stdout.write("Windows execution is disabled")
            return
        limit = max(1, min(options["limit"], 100))
        rows = ExeSubmission.objects.filter(config_snapshot__assignment_mode="broker_pool")
        ids = (
            rows.filter(status="ASSIGNED", finished_at__isnull=True)
            .order_by("created_at")
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
