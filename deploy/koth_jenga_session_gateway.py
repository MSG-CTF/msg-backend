"""Add ordinary Jenga cookie authorization inside its existing release branch."""
import re
from urllib.parse import urlsplit
from uuid import UUID

MARKER = "msg-koth-jenga-native-session-v1"


def guard_jenga_sessions(original, challenge_id, origin):
    if str(UUID(challenge_id)) != challenge_id:
        raise ValueError("Canonical challenge UUID required")
    parsed = urlsplit(origin)
    if (parsed.scheme != "https" or parsed.netloc != parsed.hostname or parsed.path
            or not re.fullmatch(r"[a-z0-9.-]+", parsed.hostname or "")):
        raise ValueError("Canonical HTTPS origin required")
    if MARKER in original:
        raise ValueError("Session guard already installed")
    # Operator previews retain their separate grant and native-session boundary.
    releases = list(re.finditer(r"(?m)^(\s*)@released \{\s*$", original))
    handles = list(re.finditer(r"(?m)^([ \t]*)handle @released \{\s*$", original))
    if len(releases) != 1 or len(handles) != 1 or releases[0].start() >= handles[0].start():
        raise ValueError("Expected one existing release matcher and handler")
    start = releases[0].end(); end = handles[0].start()
    matcher = original[start:end]
    matcher, count = re.subn(r"expression `([^`]+)`", r'expression `(\1) && {http.request.cookie.__Host-msg_koth_preview} == ""`', matcher)
    if count != 1:
        raise ValueError("Expected the existing time window")
    original = original[:start] + matcher + original[end:]
    handle = re.search(r"(?m)^([ \t]*)handle @released \{\s*$", original)
    indent = handle[1] + "\t"
    lines = [f"# {MARKER}",
             "@jenga_native_protected not path / /auth /healthz /favicon.ico /static/* /assets/*",
             "forward_auth @jenga_native_protected https://msgctf.kr {",
             " uri /api/v1/koth/native_sessions/jenga_authorize",
             f" header_up X-KOTH-Challenge {challenge_id}", f" header_up X-KOTH-Origin {origin}",
             " header_up X-KOTH-Request-Method {http.request.orig_method}",
             ' header_up Cookie "session={http.request.cookie.session}"',
             " header_up -Authorization", " transport http {", "  dial_timeout 2s", "  response_header_timeout 5s", " }", "}"]
    block = "\n" + "\n".join(indent + line for line in lines)
    return original[:handle.end()] + block + original[handle.end():]
