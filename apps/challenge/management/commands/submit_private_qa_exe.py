import json
import uuid
from pathlib import Path

from django.core.files import File
from django.core.management.base import BaseCommand, CommandError

from apps.accounts.models import User
from apps.challenge.exe_submissions import ExeSubmissionError, accept_exe


class Command(BaseCommand):
    help = "Store one approved private Windows QA submission without dispatching it."

    def add_arguments(self, parser):
        parser.add_argument("--user-id", type=uuid.UUID, required=True)
        parser.add_argument("--team-id", type=uuid.UUID, required=True)
        parser.add_argument("--challenge-id", type=uuid.UUID, required=True)
        parser.add_argument("--file", type=Path, required=True)
        parser.add_argument("--sha256", required=True)
        parser.add_argument("--idempotency-key", type=uuid.UUID, required=True)
        parser.add_argument(
            "--approved-by", required=True, help="Approving administrator login ID."
        )
        parser.add_argument(
            "--approval-reference",
            required=True,
            help="Reference to the recorded operator approval.",
        )
        parser.add_argument("--checker-commit-sha", required=True)
        parser.add_argument("--config-version", required=True)

    def handle(self, *args, **options):
        user = User.objects.filter(pk=options["user_id"]).first()
        if user is None:
            raise CommandError("QA_USER_NOT_FOUND")
        approval = {
            "user_id": str(options["user_id"]),
            "team_id": str(options["team_id"]),
            "approved_by": options["approved_by"],
            "reference": options["approval_reference"].strip(),
            "payload_sha256": options["sha256"],
            "checker_commit_sha": options["checker_commit_sha"],
            "config_version": options["config_version"],
        }
        path = Path(options["file"])
        try:
            with path.open("rb") as source:
                row, created = accept_exe(
                    user,
                    options["challenge_id"],
                    str(options["idempotency_key"]),
                    File(source, name=path.name),
                    qa_approval=approval,
                )
        except ExeSubmissionError as error:
            raise CommandError(error.code) from None
        except OSError:
            raise CommandError("QA_FILE_UNAVAILABLE") from None
        self.stdout.write(
            json.dumps(
                {
                    "submission_id": str(row.pk),
                    "created": created,
                    "challenge_id": str(row.challenge_id),
                    "user_id": str(row.user_id),
                    "team_id": str(row.team_id),
                    "payload_sha256": row.payload_sha256,
                    "status": row.status,
                    "dispatch_state": row.dispatch_state,
                    "approval": row.config_snapshot["private_qa"],
                }
            )
        )
