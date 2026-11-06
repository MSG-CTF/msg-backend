import json

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.common.key_lifecycle import key_id, material_for
from apps.instances.models import RuntimeSecret, ServiceKeyRotation, ServiceKeyState
from apps.koth.models import KothEnvironmentVersion


class Command(BaseCommand):
    help = "Activate a service key generation and atomically reencrypt all service ciphertext"

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
        model_rows = {}
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
                primary = Fernet(material.encode())
                for model, field in ((RuntimeSecret, "encrypted_value"), (KothEnvironmentVersion, "encrypted_settings")):
                    count = 0
                    for record in model.objects.select_for_update().order_by("pk").iterator():
                        rotated = cipher.rotate(getattr(record, field).encode()).decode()
                        primary.decrypt(rotated.encode())
                        setattr(record, field, rotated)
                        record.save(update_fields=[field])
                        count += 1
                    model_rows[model._meta.label] = count
                    rows += count
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
                    "reencrypted_models": model_rows,
                    "active_key_id": version,
                }
            )
        )
