# Instance Write Recovery (F32 / F33)

Apply `instances.0020_instance_write_operation` before deploying Backend web.
Public request/response fields and Scheduler endpoints are unchanged. No timer
is installed or enabled by this change.

Participant create/reset commits an `InstanceWriteOperation` before calling
Scheduler. Local result persistence and operation completion commit together.
If persistence or its commit fails, the original operation survives and the
known remote instance ID is retained for recovery. Pending and uncertain
operations block another participant create/reset and administrator reset for
that user; unrelated users can continue.

Inspect incomplete operations without calling Scheduler:

```sh
python manage.py recover_instance_writes --show
```

Recover one approved operation in the same Compose project/container/database
as the web process:

```sh
python manage.py recover_instance_writes --operation-id <operation-uuid>
```

Recovery GETs only the recorded remote ID, verifies owner/challenge/release,
and persists the current state using the original release. It never POSTs
create/reset, deletes remote resources, activates a release, or changes an
unrelated instance. Completed recovery is idempotent. Existing local instances
are not overwritten with a potentially older network snapshot.
`COMPLETED` describes the local write, not workload success or resource cleanup.

`RECOVERY_PENDING` can be retried with the same command. `REVIEW_REQUIRED`
needs operator verification: a 404, missing ownership evidence or version
mismatch is not proof of cleanup. After correcting the cause, explicitly select
the same operation ID to recheck a known remote ID. This never bypasses scope
validation. If the process was interrupted before the
remote ID was saved, `STARTED` remains visible and blocked; never resend the
original create/reset until Scheduler's actual result is established. No
automatic release of these safeguards is provided by this command.

If the database stays unavailable, the pre-call journal remains the last
durable record and a failed update logs only operation/remote IDs. Investigate
that operation rather than creating a replacement. The command exits nonzero
when selected operations remain unresolved, except in read-only `--show` mode.
