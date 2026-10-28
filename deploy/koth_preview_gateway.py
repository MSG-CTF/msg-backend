"""Render the two authenticated preview paths into an existing KOTH Caddy route."""
import base64
import hashlib
import ipaddress
import re
from urllib.parse import urlsplit
from uuid import UUID


def render_preview_gateway(original, challenge_id, origin, upstream, bootstrap):
    if str(UUID(challenge_id)) != challenge_id:
        raise ValueError("canonical challenge UUID required")
    parsed = urlsplit(origin)
    if (parsed.scheme != "https" or parsed.netloc != parsed.hostname or parsed.path
            or not re.fullmatch(r"[a-z0-9.-]+", parsed.hostname or "")):
        raise ValueError("canonical HTTPS origin required")
    host, port = upstream.rsplit(":", 1)
    ipaddress.ip_address(host)
    if not port.isdecimal() or not 1 <= int(port) <= 65535:
        raise ValueError("valid private upstream required")
    if "msg-koth-admin-preview-v1" in original:
        raise ValueError("preview already installed; inspect before replacing")
    script = bootstrap.split("<script>", 1)[1].split("</script>", 1)[0]
    digest = base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()
    entry = f'''
  # msg-koth-admin-preview-v1 entry begin
  @preview_entry_get {{
   path /__operator/preview
   method GET
  }}
  handle @preview_entry_get {{
   header Cache-Control "no-store, private"
   header Content-Security-Policy "default-src 'none'; script-src 'sha256-{digest}'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'"
   root * /etc/msgctf-koth
   rewrite * /admin-preview.html
   file_server
  }}
  @preview_entry_post {{
   path /__operator/preview
   method POST
  }}
  handle @preview_entry_post {{
   request_body {{
    max_size 4KB
   }}
   reverse_proxy https://msgctf.kr {{
    rewrite /api/v1/koth/preview_sessions/authorize
    header_up X-KOTH-Preview-Challenge {challenge_id}
    header_up X-KOTH-Preview-Origin {origin}
    header_up -Cookie
    header_up -Authorization
    transport http {{
     dial_timeout 2s
     response_header_timeout 3s
    }}
   }}
  }}
  # msg-koth-admin-preview-v1 entry end
'''
    session = f'''
  # msg-koth-admin-preview-v1 session begin
  @preview_session expression `{{http.request.cookie.__Host-msg_koth_preview}} != ""`
  handle @preview_session {{
   forward_auth https://msgctf.kr {{
    uri /api/v1/koth/preview_sessions/authorize
    header_up X-KOTH-Preview-Challenge {challenge_id}
    header_up X-KOTH-Preview-Origin {origin}
    header_up Cookie "__Host-msg_koth_preview={{http.request.cookie.__Host-msg_koth_preview}}"
    header_up -Authorization
    transport http {{
     dial_timeout 2s
     response_header_timeout 3s
    }}
   }}
   reverse_proxy {upstream} {{
    transport http {{
     response_header_timeout 160s
    }}
   }}
  }}
  # msg-koth-admin-preview-v1 session end
'''
    # Keep internal-path denial, source-IP checks and the release window in place.
    original, count = re.subn(r"(?m)^(\s*)@internal path ", lambda m:entry + m.group(0), original, count=1)
    if count != 1:
        raise ValueError("existing internal-path boundary not found")
    original, count = re.subn(r'(?m)^(\s*)respond "?KOTH_NOT_OPEN"? 403', lambda m:session + m.group(0), original, count=1)
    if count != 1:
        raise ValueError("existing release boundary not found")
    # Never expose the administrator's grant to a deliberately vulnerable game.
    original, count = re.subn(r"(reverse_proxy " + re.escape(upstream) + r"\s*\{)",
        lambda m:m.group(1) + '\n header_up Cookie "(^|; *)__Host-msg_koth_preview=[^;]*" ""', original)
    if count < 2:
        raise ValueError("game proxy boundaries not found")
    return original


GAME_PROFILES = {
    "koth-card-battle-arena": ("/api/session", "", True),
    "koth-jenga": ("/auth", "", False),
    "koth-swanopoly": ("/api/team/login", "", False),
    "koth-pixel-smuggler": ("/api/v1/session", "/api/v1/session", False),
    "koth-grand-tour-duel": ("/api/team/session", "/api/team/session", False),
    "koth-dependency-hell": ("/api/v1/session", "/api/v1/session", False),
}
GAME_PUBLIC_ASSETS = {
    "koth-swanopoly": "/common.js /images/* /login.css /login.js /style.css",
    "koth-dependency-hell": "/app.js /styles.css /api/v1/leaderboard /downloads/incident-analyzer /downloads/dependency-hell-for-user.tar.gz",
    "koth-pixel-smuggler": "/api/v1/round /api/v1/leaderboard /api/v1/rounds/*/reference.png /api/v1/rounds/*/target.png",
    "koth-grand-tour-duel": "/team/login /problem /card-replay /api/card/arena /api/card/rating-rules",
}


def upgrade_game_preview(original, challenge_id, origin, upstream, game_slug,
                         *, qa_cookie_value=None):
    """Upgrade only the operator preview branch of an installed v1 gateway.

    qa_cookie_value creates an additional, isolated candidate route for an
    operator test. The released route, IP checks and score boundary are kept.
    """
    if str(UUID(challenge_id)) != challenge_id:
        raise ValueError("canonical challenge UUID required")
    parsed = urlsplit(origin)
    if (parsed.scheme != "https" or parsed.netloc != parsed.hostname or parsed.path
            or not re.fullmatch(r"[a-z0-9.-]+", parsed.hostname or "")):
        raise ValueError("canonical HTTPS origin required")
    host, port = upstream.rsplit(":", 1)
    address = ipaddress.ip_address(host)
    if (not address.is_private or address.is_unspecified or address.is_multicast
            or not port.isdecimal() or not 1 <= int(port) <= 65535):
        raise ValueError("valid private upstream required")
    try:
        login, logout, card = GAME_PROFILES[game_slug]
    except KeyError:
        raise ValueError("unsupported KOTH preview game") from None
    starts = list(re.finditer(r"(?m)^[ \t]*# msg-koth-admin-preview-v1 session begin[ \t]*$", original))
    ends = list(re.finditer(r"(?m)^[ \t]*# msg-koth-admin-preview-v1 session end[ \t]*$", original))
    if len(starts) != 1 or len(ends) != 1 or ends[0].start() <= starts[0].end():
        raise ValueError("exactly one installed v1 preview session branch required")
    matcher = '{http.request.cookie.__Host-msg_koth_preview} != ""'
    marker = "msg-koth-admin-preview-game-v2"
    if qa_cookie_value is not None:
        if not re.fullmatch(r"[a-zA-Z0-9_-]{32,128}", qa_cookie_value):
            raise ValueError("bounded random QA cookie required")
        matcher = '{http.request.cookie.__Host-msg_koth_native_qa} == "' + qa_cookie_value + '"'
        marker += "-qa"
    if marker in original:
        raise ValueError("preview game route already installed")

    def headers(uri=False):
        value = (f"header_up X-KOTH-Preview-Challenge {challenge_id}\n"
                 f"header_up X-KOTH-Preview-Origin {origin}\n")
        if uri:
            value += "header_up X-KOTH-Preview-URI {http.request.orig_uri}\n"
            value += "header_up X-KOTH-Preview-Method {http.request.orig_method}\n"
        return value

    transport = "transport http {\n dial_timeout 2s\n response_header_timeout 12s\n}\n"
    native = (f"reverse_proxy {upstream} {{\n"
              'header_up Cookie "(^|; *)__Host-msg_koth_[^=;]+=[^;]*" ""\n'
              "transport http {\n response_header_timeout 160s\n}\n}\n")
    proxy = ("reverse_proxy https://msgctf.kr {\n"
             "rewrite /api/v1/koth/preview_sessions/game_proxy\n"
             + headers(True) + transport + "}\n")
    public = ("forward_auth https://msgctf.kr {\n"
              "uri /api/v1/koth/preview_sessions/authorize\n"
              + headers() + 'header_up Cookie "__Host-msg_koth_preview={http.request.cookie.__Host-msg_koth_preview}"\n'
              "header_up -Authorization\n" + transport + "}\n" + native)
    protected = proxy if card else (
        "forward_auth https://msgctf.kr {\n"
        "uri /api/v1/koth/preview_sessions/game_authorize\n"
        + headers(True) + transport + "}\n" + native)
    block = (f"\n# {marker} begin\n@preview_game expression `{matcher}`\n"
             "handle @preview_game {\nroute {\n"
             "@preview_game_public {\n method GET HEAD\n"
             "path / /healthz /favicon.ico /config.js /static/* /assets/* /api/cards "
             + GAME_PUBLIC_ASSETS.get(game_slug, "") + "\n}\n"
             "handle @preview_game_public {\n" + public + "}\n"
             f"@preview_game_login {{\nmethod POST\npath {login}\n}}\n"
             "handle @preview_game_login {\n" + proxy + "}\n")
    if logout:
        block += (f"@preview_game_logout {{\nmethod DELETE\npath {logout}\n}}\n"
                  "handle @preview_game_logout {\n" + proxy + "}\n")
    block += "handle {\n" + protected + f"}}\n}}\n}}\n# {marker} end\n"
    if qa_cookie_value is not None:
        return original[:starts[0].start()] + block + original[starts[0].start():]
    start = starts[0].start()
    finish = ends[0].end()
    return original[:start] + block + original[finish:]
