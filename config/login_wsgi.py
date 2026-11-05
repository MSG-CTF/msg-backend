"""Login-only worker pool; authentication still runs the ordinary Django view."""
import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django_application = get_wsgi_application()


def application(environ, start_response):
    path = environ.get("PATH_INFO", "")
    if path == "/_internal/login-ready" and environ.get("REQUEST_METHOD") == "GET":
        start_response("200 OK", [("Content-Type", "text/plain"), ("Content-Length", "2")])
        return [b"ok"]
    if path != "/api/v1/auth/login":
        start_response("404 Not Found", [("Content-Length", "0")])
        return [b""]
    return django_application(environ, start_response)
