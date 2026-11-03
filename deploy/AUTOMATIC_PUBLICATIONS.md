# CI publication activation

Set AUTO_ACTIVATE_PUBLISHED_RELEASES=True together with the existing
IMAGE_PREPARATION_ENABLED=True and IMAGE_PREPARATION_ACTIVATE_WHEN_READY=True.
AUTO_ACTIVATE_RELEASE_WORKFLOW defaults to .github/workflows/challenge-validation.yml.
The trusted repository and branch remain RELEASE_POLL_REPO and RELEASE_POLL_SOURCE_REF.
Migrate before enabling this option. Install the Runtime publisher service before
enabling it; see secure-provisioner/deploy/PUBLISHED_IMAGES.md.

poll_releases records a durable AutomaticPublication after independently verified
successful main CI, exact source SHA and matching images. The periodic
process_image_preparations command also reconciles these records, so an admin
request is not needed to move the pipeline forward. The same revision's
participant-file record and repository environment must be complete. Empty
participant-file publications are valid; present files need a stored object key.

Legacy WEB/PWN DOCKER is the existing Scheduler Kubernetes alias. CI public-port
defaults are not an operator privilege change: Runtime always replaces exposed
ports with the reviewed image policy. The CI manifest remains unchanged. Actual
port-number, isolation-profile, architecture or probe changes are still held.

An existing reviewed Kubernetes execution profile is required. Port/profile or
probe changes and missing environment are held for correction. The existing
preparation outbox downloads the exact images onto every configured target.
Only fresh, complete readiness activates the version. A failed preparation leaves
the previous version active and records its error in the preparation/publication
ledger. A Runtime policy-sync 422 is retried with the same request for at most
30 minutes; permanent manual-policy failures keep their current behavior.

Challenge locks, captured current-release IDs and activation generations protect
against duplicate polls, overlapping publications and explicit operator rollbacks.
Automatic transitions advance another pending publication's fence only if it
still matches; operator transitions do not. Already terminal publications are not
replayed. Approval and activation keep the existing admin audit events.

The option defaults to False to avoid changing a site's rollout policy during a
code-only deployment. Production must enable it once; routine image publications
then require no per-version approval or manual .env import.
