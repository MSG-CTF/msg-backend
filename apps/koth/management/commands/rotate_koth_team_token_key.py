"""Activate or inspect a KOTH service key without accepting key material in argv."""

import json

from django.core.management.base import BaseCommand, CommandError
from django.db import DatabaseError

from apps.koth.models import KothTokenKeyRotation, KothTokenKeyState
from apps.koth.token_keys import (
    activate_key,
    key_fingerprint,
    KeyRotationError,
    receipt_metadata,
)


class Command(BaseCommand):
    help = "Register the configured KOTH key or inspect redacted rotation receipts"

    def add_arguments(self, parser):
        parser.add_argument("--status", action="store_true")
        parser.add_argument("--actor")
        parser.add_argument("--reason")
        parser.add_argument("--expected-version", type=int)
        parser.add_argument("--operation-id")

    def handle(self, *args, **options):
        try:
            if options["status"]:
                if any(
                    options[name] is not None
                    for name in ["actor", "reason", "expected_version", "operation_id"]
                ):
                    raise CommandError(
                        "--status cannot be combined with cutover arguments"
                    )
                state = KothTokenKeyState.objects.filter(pk=1).first()
                result = {
                    "active_version": state.version if state else 0,
                    "configured_key_active": bool(
                        state
                        and state.version
                        and state.fingerprint == key_fingerprint()
                    ),
                    "history": [
                        receipt_metadata(row)
                        for row in KothTokenKeyRotation.objects.order_by("-version")[
                            :100
                        ]
                    ],
                }
            else:
                if any(
                    options[name] is None
                    for name in ["actor", "reason", "expected_version", "operation_id"]
                ):
                    raise CommandError(
                        "actor, reason, expected-version and operation-id are required"
                    )
                receipt, created = activate_key(
                    actor=options["actor"],
                    reason=options["reason"],
                    expected_version=options["expected_version"],
                    operation_id=options["operation_id"],
                )
                state = KothTokenKeyState.objects.get(pk=1)
                result = {
                    **receipt_metadata(receipt),
                    "created": created,
                    "active_version": state.version,
                    "configured_key_active": state.fingerprint == key_fingerprint(),
                }
        except KeyRotationError as error:
            raise CommandError(str(error)) from None
        except DatabaseError:
            raise CommandError(
                "Key state and audit receipt could not be committed; inspect status before retrying"
            ) from None
        self.stdout.write(json.dumps(result, ensure_ascii=False, sort_keys=True))
