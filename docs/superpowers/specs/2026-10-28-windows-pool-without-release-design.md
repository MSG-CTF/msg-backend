# Windows broker pool submissions without a Backend release

For `pwn-msgdriver`, Broker selects a prepared Windows VM. A Backend `ChallengeRelease` does not identify that VM or prove its image, and no Windows release can be registered through the container-only API. An active release must therefore not gate the Windows EXE path.

New `broker_pool` submissions keep `release_id=NULL`. Backend still requires the configured logical pool, service authentication, private payload storage, valid FLAG secret, per-challenge enablement, publication, and team access. Existing submissions retain their release FK. The job token and Runtime POST omit `release_id` for the release-free path. Legacy fixed-VM checker submissions still require a valid release UUID, preserving their contract. Runtime accepts an optional valid UUID on historical pool jobs so an in-flight submission from the previous contract can finish; the ID is never used to select a VM.

The API returns the same submission ID and later RESULT fields. This change does not assert a particular Windows image or inject a FLAG. Broker/VM ownership and recovery remain separate. The `web-daily-point` image-agent failure concerns Kubernetes image prefetch and is independent of this Windows execution path.
