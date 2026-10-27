# Windows Pool Without Release Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Allow `pwn-msgdriver` EXE submission and RESULT delivery using Broker's prepared VM without an unrelated Backend active release.

**Architecture:** Backend stores a nullable historical release FK, omits release identity from new `broker_pool` requests and JWTs, and drops its Windows readiness gate. Runtime validates release identity by selection mode. Existing fixed-VM requests retain their release requirement.

**Tech Stack:** Django 5.2/DRF/PostgreSQL and Go.

**Spec:** `docs/superpowers/specs/2026-10-28-windows-pool-without-release-design.md`

## Global Constraints

- Do not invent a release UUID or alter existing release records.
- Keep pool, auth, FLAG-secret, challenge publication, team access, and payload protections.
- Preserve existing legacy checker request and token rules.

## Review Focus

- A release-free pool submission can be accepted, dispatched, and produce a RESULT.
- An unrelated or omitted pool config remains rejected.
- A release claim on a new release-free job token is rejected; an old bound release claim remains valid for historical rows.
- Legacy fixed-VM requests without release UUID remain rejected.
- Existing rows with release IDs still read and replay correctly.

---

### Task 1: Backend release-free submission

**Files:** `apps/challenge/models.py`, migration, `remote_run_config.py`, `exe_submissions.py`, tests.

- [x] Write a failing test for a published, enabled pool challenge with no active release.
- [x] Make the release FK nullable and remove the Windows readiness gate.
- [x] Run focused tests and migration check.

### Task 2: Backend wire and token contract

**Files:** `checker_dispatch.py`, `checker_auth.py`, Windows tests.

- [x] Write failing tests that new pool POST/JWT omit release ID while callbacks accept the bound token.
- [x] Implement mode-specific release claim validation.
- [x] Run callback and result tests.

### Task 3: Runtime contract and documentation

**Files:** Runtime `internal/windowsruntime/checker.go`, pool tests, Backend Windows runbook.

- [x] Write failing Go tests for release-free pool acceptance and fixed-VM missing-release rejection; retain old pool job compatibility.
- [x] Make validation mode-specific and update documentation.
- [x] Run Go package and Backend Windows tests; report actual E2E availability separately.
