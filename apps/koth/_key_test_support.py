"""Explicit key registration for isolated test databases, using the real cutover."""

import uuid

from .token_keys import activate_key


def initialize_token_key():
    receipt, _ = activate_key(
        actor="test-operator",
        reason="isolated test key registration",
        expected_version=0,
        operation_id=uuid.uuid4(),
    )
    return receipt.version
