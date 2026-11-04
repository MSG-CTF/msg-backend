"""Compatibility name for the audited key-generation command.

The old unversioned purge flag is intentionally unsupported. All callers must
provide the operation ID, expected generation, operator and reason.
"""

from .rotate_koth_team_token_key import Command  # noqa: F401
