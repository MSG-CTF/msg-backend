"""One container, separate API/login pools, and a bounded nginx front door."""
import http.client
import os
from pathlib import Path
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time

RUNTIME = Path(tempfile.gettempdir()) / "msg-backend-web"


def integer(environment, name, default, minimum, maximum):
    value = int(environment.get(name, str(default)))
    if not minimum <= value <= maximum:
        raise ValueError(f"Invalid {name}")
    return value


def prepare_runtime(directory=RUNTIME):
    directory.mkdir(mode=0o700, exist_ok=True)
    metadata = directory.lstat()
    if (not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid()
            or metadata.st_mode & 0o077 or directory.resolve() != directory):
        raise ValueError("Unsafe web runtime directory")
    for name in ("api.sock", "login.sock", "api-control.sock", "login-control.sock"):
        path = directory / name
        if path.exists() or path.is_symlink():
            entry = path.lstat()
            if not stat.S_ISSOCK(entry.st_mode) or entry.st_uid != os.geteuid():
                raise ValueError("Unexpected web runtime entry")
            path.unlink()


def nginx_configuration(port, directory=RUNTIME):
    return f"""pid {directory}/nginx.pid;
error_log /dev/stderr warn;
worker_processes 1;
events {{ worker_connections 2048; }}
http {{
    access_log off;
    client_body_temp_path {directory}/client-body;
    proxy_temp_path {directory}/proxy;
    fastcgi_temp_path {directory}/fastcgi;
    uwsgi_temp_path {directory}/uwsgi;
    scgi_temp_path {directory}/scgi;
    client_max_body_size 0;
    upstream api_workers {{ server unix:{directory}/api.sock; keepalive 32; }}
    upstream login_workers {{ server unix:{directory}/login.sock; keepalive 4; }}
    server {{
        listen {port};
        server_name _;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_set_header Host $http_host;
        proxy_set_header X-Forwarded-Proto $http_x_forwarded_proto;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_buffering off;
        proxy_request_buffering off;
        proxy_read_timeout 900s;
        proxy_send_timeout 900s;
        location = /_internal/api-ready {{ return 404; }}
        location = /_internal/login-ready {{ return 404; }}
        location = /api/v1/auth/login {{
            client_max_body_size 8k;
            proxy_request_buffering on;
            proxy_pass http://login_workers;
        }}
        location / {{ proxy_pass http://api_workers; }}
    }}
}}
"""


def commands(environment, directory=RUNTIME):
    workers = integer(environment, "WEB_CONCURRENCY", 2, 1, 32)
    login_workers = integer(environment, "LOGIN_WEB_CONCURRENCY", 1, 1, 8)
    threads = integer(environment, "GUNICORN_THREADS", 4, 1, 64)
    timeout = integer(environment, "GUNICORN_TIMEOUT", 30, 1, 900)
    keepalive = integer(environment, "GUNICORN_KEEPALIVE", 10, 0, 300)
    common = ["--worker-class", "gthread", "--keep-alive", str(keepalive),
              "--timeout", str(timeout), "--access-logfile", "-", "--error-logfile", "-"]
    return [
        [sys.executable, "-m", "gunicorn", "config.api_wsgi:application", "--bind",
         "unix:"+str(directory/"api.sock"), "--control-socket", str(directory/"api-control.sock"),
         "--workers", str(workers), "--threads", str(threads), *common],
        [sys.executable, "-m", "gunicorn", "config.login_wsgi:application", "--bind",
         "unix:"+str(directory/"login.sock"), "--control-socket", str(directory/"login-control.sock"),
         "--workers", str(login_workers), "--threads", "1", *common],
    ]


class UnixConnection(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__("localhost", timeout=1)
        self.path = str(path)

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


def ready(path, request_path):
    connection = UnixConnection(path)
    try:
        host = os.environ.get("DJANGO_ALLOWED_HOSTS", "localhost").split(",")[0].strip().lstrip(".")
        if host in ("", "*"):
            host = "localhost"
        connection.request("GET", request_path, headers={"Host": host, "X-Forwarded-Proto": "https"})
        response = connection.getresponse()
        response.read(4096)
        return response.status == 200
    except (OSError, http.client.HTTPException):
        return False
    finally:
        connection.close()


def stop(children, grace=8):
    for child in reversed(children):
        if child.poll() is None:
            try:
                os.killpg(child.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
    deadline = time.monotonic()+grace
    for child in children:
        try:
            child.wait(timeout=max(.01, deadline-time.monotonic()))
        except subprocess.TimeoutExpired:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            child.wait(timeout=5)


def supervise(directory):
    prepare_runtime(directory)
    port = integer(os.environ, "PORT", 8080, 1, 65535)
    config = directory/"nginx.conf"
    config.write_text(nginx_configuration(port, directory), encoding="utf-8")
    children = []
    stopping = False

    def terminate(signum, frame):
        nonlocal stopping
        stopping = True

    def reload_workers(signum, frame):
        for child in children:
            if child.poll() is None:
                try:
                    os.killpg(child.pid, signal.SIGHUP)
                except ProcessLookupError:
                    pass

    signal.signal(signal.SIGTERM, terminate)
    signal.signal(signal.SIGINT, terminate)
    signal.signal(signal.SIGHUP, reload_workers)
    try:
        for command in commands(os.environ, directory):
            children.append(subprocess.Popen(command, start_new_session=True))
        deadline = time.monotonic()+60
        while not stopping and time.monotonic()<deadline:
            if any(child.poll() is not None for child in children):
                raise RuntimeError("Web worker pool stopped during startup")
            if ready(directory/"api.sock", "/_internal/api-ready") and ready(directory/"login.sock", "/_internal/login-ready"):
                break
            time.sleep(.2)
        else:
            if stopping:
                return 0
            raise RuntimeError("Web worker pools did not become ready")
        children.append(subprocess.Popen(["nginx", "-e", "stderr", "-c", str(config), "-g", "daemon off;"], start_new_session=True))
        while not stopping:
            if any(child.poll() is not None for child in children):
                raise RuntimeError("Web worker pool or proxy stopped")
            time.sleep(.2)
        return 0
    except (OSError, RuntimeError, ValueError):
        print("Backend web process failed; stopping all worker pools", file=sys.stderr, flush=True)
        return 1
    finally:
        stop(children)


def main():
    with tempfile.TemporaryDirectory(prefix="msg-backend-web-") as directory:
        return supervise(Path(directory))


if __name__ == "__main__":
    raise SystemExit(main())
