"""API pool process readiness, separate from the public dependency health check."""
from .wsgi import application as django_application


def application(environ, start_response):
    if (environ.get("PATH_INFO") == "/_internal/api-ready"
            and environ.get("REQUEST_METHOD") == "GET"):
        start_response("200 OK", [("Content-Type", "text/plain"), ("Content-Length", "2")])
        return [b"ok"]
    return django_application(environ, start_response)
