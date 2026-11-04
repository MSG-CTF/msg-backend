import json

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.common.key_lifecycle import key_id, material_for
from apps.instances.models import RuntimeSecret, ServiceKeyRotation, ServiceKeyState


class Command(BaseCommand):
    help = "Activate a service key generation and atomically reencrypt runtime records"

    def add_arguments(self, parser):
        parser.add_argument("--purpose", choices=["runtime", "koth"], required=True)
        parser.add_argument("--actor", required=True)
        parser.add_argument("--reason", required=True)
        parser.add_argument("--reencrypt", action="store_true")
        parser.add_argument("--expected-version", type=int)
        parser.add_argument("--operation-id")

    @transaction.atomic
    def handle(self, *args, **options):
        purpose = options["purpose"]
        if purpose == "koth":
            from django.core.management import call_command

            if options["reencrypt"]:
                raise CommandError("KOTH activation does not reencrypt runtime data")
            if options["expected_version"] is None or not options["operation_id"]:
                raise CommandError(
                    "KOTH activation requires expected-version and operation-id; use rotate_koth_team_token_key"
                )
            call_command(
                "rotate_koth_team_token_key",
                actor=options["actor"],
                reason=options["reason"],
                expected_version=options["expected_version"],
                operation_id=options["operation_id"],
                stdout=self.stdout,
            )
            return
        if options["expected_version"] is not None or options["operation_id"]:
            raise CommandError(
                "KOTH cutover arguments cannot be used for runtime activation"
            )
        actor, reason = options["actor"].strip(), options["reason"].strip()
        if not actor or len(actor) > 100 or not reason or len(reason) > 250:
            raise CommandError(
                "Actor and reason are required and must fit the audit fields"
            )
        material = material_for(purpose)
        if not material or len(material.strip().encode()) < 32:
            raise CommandError("A separate nonempty service key is required")
        version = key_id(purpose, material)
        state, created = ServiceKeyState.objects.get_or_create(
            purpose=purpose,
            defaults={
                "active_key_id": version,
                "generation": 1,
                "activated_at": timezone.now(),
            },
        )
        state = ServiceKeyState.objects.select_for_update().get(pk=purpose)
        previous = "" if created else state.active_key_id
        if not created and previous == version:
            self.stdout.write(
                json.dumps(
                    {
                        "purpose": purpose,
                        "generation": state.generation,
                        "changed": False,
                    }
                )
            )
            return
        if (
            not created
            and ServiceKeyRotation.objects.filter(
                purpose=purpose, active_key_id=version
            ).exists()
        ):
            raise CommandError("Retired service key versions cannot be reactivated")
        rows = 0
        if purpose == "runtime":
            if not options["reencrypt"]:
                raise CommandError("Runtime activation requires --reencrypt")
            try:
                cipher = MultiFernet(
                    [
                        Fernet(value.encode())
                        for value in settings.RUNTIME_SECRET_ENCRYPTION_KEYS
                    ]
                )
                for secret in (
                    RuntimeSecret.objects.select_for_update().order_by("pk").iterator()
                ):
                    rotated = cipher.rotate(secret.encrypted_value.encode()).decode()
                    # Verify that retiring all old keys will preserve this record.
                    Fernet(material.encode()).decrypt(rotated.encode())
                    secret.encrypted_value = rotated
                    secret.save(update_fields=["encrypted_value"])
                    rows += 1
            except (InvalidToken, ValueError, TypeError):
                raise CommandError(
                    "Key activation aborted; no rows or key state were committed"
                ) from None
        state.active_key_id = version
        state.generation = 1 if created else state.generation + 1
        state.activated_at = timezone.now()
        state.save()
        ServiceKeyRotation.objects.create(
            purpose=purpose,
            previous_key_id=previous,
            active_key_id=version,
            generation=state.generation,
            reencrypted_rows=rows,
            actor=actor,
            reason=reason,
        )
        self.stdout.write(
            json.dumps(
                {
                    "purpose": purpose,
                    "generation": state.generation,
                    "changed": True,
                    "reencrypted_rows": rows,
                    "active_key_id": version,
                }
            )
        )
