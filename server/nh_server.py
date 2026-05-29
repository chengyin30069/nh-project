#!/usr/bin/env python3
"""LAN-only HTTP API for queueing nh-project downloads."""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import queue
import re
import shutil
import subprocess
import threading
import time
import uuid
import zipfile
from collections import deque
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import urlparse


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ALLOWED_NETWORKS = (
    "192.168.50.0/24",
    "192.168.193.0/24",
    "127.0.0.1/32",
    "::1/128",
)
TERMINAL_STATUSES = {"succeeded", "failed"}
GALLERY_ID_RE = re.compile(r"^[0-9]+$")


def is_valid_gallery_id(value: object) -> bool:
    return isinstance(value, str) and bool(GALLERY_ID_RE.fullmatch(value))


def parse_networks(values: Iterable[str]) -> tuple[ipaddress._BaseNetwork, ...]:
    return tuple(ipaddress.ip_network(value, strict=False) for value in values)


def is_ip_allowed(address: str, networks: Iterable[ipaddress._BaseNetwork]) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return any(ip in network for network in networks)


def expand_path(value: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(value))).resolve()


def load_cookie_env(cookie_file: Path, base_env: dict[str, str] | None = None) -> dict[str, str]:
    """Load cookie.sh into an env dict without exposing secrets as argv."""

    env = dict(base_env or os.environ)
    if not cookie_file.exists():
        return env

    source_env = dict(env)
    source_env["NH_COOKIE_FILE"] = str(cookie_file)
    result = subprocess.run(
        ["bash", "-lc", 'set -a; source "$NH_COOKIE_FILE"; set +a; env -0'],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=source_env,
        check=False,
    )
    if result.returncode != 0:
        message = result.stderr.decode("utf-8", "replace").strip()
        raise RuntimeError(f"failed to load {cookie_file}: {message}")

    loaded: dict[str, str] = {}
    for item in result.stdout.split(b"\0"):
        if not item or b"=" not in item:
            continue
        key, value = item.split(b"=", 1)
        loaded[key.decode("utf-8", "replace")] = value.decode("utf-8", "replace")
    return loaded


@dataclass
class Job:
    job_id: str
    gallery_id: str
    status: str = "queued"
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    return_code: int | None = None
    error: str | None = None
    logs: deque[str] = field(default_factory=lambda: deque(maxlen=80))

    def to_dict(self) -> dict[str, object]:
        return {
            "job_id": self.job_id,
            "id": self.gallery_id,
            "status": self.status,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "return_code": self.return_code,
            "error": self.error,
            "logs": list(self.logs),
        }


class DownloadManager:
    def __init__(
        self,
        *,
        project_root: Path = PROJECT_ROOT,
        script_path: Path | None = None,
        storage_dir: Path | None = None,
        env: dict[str, str] | None = None,
        downloader_command: list[str] | None = None,
        autostart: bool = True,
    ) -> None:
        self.project_root = project_root
        self.script_path = script_path or project_root / "nh2_requireCfToken.sh"
        self.env = dict(env or os.environ)
        self.env.setdefault("NH_FOLDER_PATH", str(Path.home() / "nh"))
        self.env.setdefault("NH_MAX_RETRY", "20")
        self.env.setdefault("NH_MEDIA_SERVER_LIST", "1 2 3 4 5 6 7 8 9")
        self.env.setdefault("NH_PARALLEL", "100")
        self.storage_dir = storage_dir or expand_path(self.env["NH_FOLDER_PATH"])
        self.downloader_command = downloader_command

        self.jobs: dict[str, Job] = {}
        self.active_by_gallery_id: dict[str, str] = {}
        self.lock = threading.RLock()
        self.queue: queue.Queue[Job] = queue.Queue()
        self.worker: threading.Thread | None = None
        if autostart:
            self.start()

    def start(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        self.worker = threading.Thread(target=self._worker_loop, name="nh-download-worker", daemon=True)
        self.worker.start()

    def submit(self, gallery_id: str) -> tuple[Job, bool]:
        if not is_valid_gallery_id(gallery_id):
            raise ValueError("gallery id must contain digits only")

        with self.lock:
            existing_id = self.active_by_gallery_id.get(gallery_id)
            if existing_id:
                existing = self.jobs[existing_id]
                if existing.status not in TERMINAL_STATUSES:
                    return existing, False

            job = Job(job_id=uuid.uuid4().hex, gallery_id=gallery_id)
            archive_path = self.archive_path(gallery_id)
            if archive_path.exists():
                job.status = "succeeded"
                job.finished_at = time.time()
                job.logs.append(f"{archive_path.name} already exists")
                self.jobs[job.job_id] = job
                self.active_by_gallery_id[gallery_id] = job.job_id
                return job, True

            self.jobs[job.job_id] = job
            self.active_by_gallery_id[gallery_id] = job.job_id
            self.queue.put(job)
            return job, True

    def get_job(self, job_id: str) -> Job | None:
        with self.lock:
            return self.jobs.get(job_id)

    def gallery_status(self, gallery_id: str) -> dict[str, object]:
        if not is_valid_gallery_id(gallery_id):
            raise ValueError("gallery id must contain digits only")

        with self.lock:
            job_id = self.active_by_gallery_id.get(gallery_id)
            job = self.jobs.get(job_id) if job_id else None
            return {
                "id": gallery_id,
                "downloaded": self.archive_path(gallery_id).exists(),
                "job_id": job.job_id if job else None,
                "status": job.status if job else None,
            }

    def galleries_status(self, gallery_ids: Iterable[str]) -> dict[str, dict[str, object]]:
        return {gallery_id: self.gallery_status(gallery_id) for gallery_id in gallery_ids}

    def archive_path(self, gallery_id: str) -> Path:
        return self.storage_dir / f"{gallery_id}.cbz"

    def folder_path(self, gallery_id: str) -> Path:
        return self.storage_dir / gallery_id

    def _worker_loop(self) -> None:
        while True:
            job = self.queue.get()
            try:
                self._run_job(job)
            finally:
                self.queue.task_done()

    def _append_log(self, job: Job, message: str) -> None:
        line = message.rstrip()
        if not line:
            return
        with self.lock:
            job.logs.append(line)

    def _set_status(self, job: Job, status: str, *, error: str | None = None) -> None:
        with self.lock:
            job.status = status
            if status == "running":
                job.started_at = time.time()
            if status in TERMINAL_STATUSES:
                job.finished_at = time.time()
            if error:
                job.error = error

    def _run_job(self, job: Job) -> None:
        self._set_status(job, "running")
        archive = self.archive_path(job.gallery_id)
        if archive.exists():
            self._append_log(job, f"{archive.name} already exists")
            self._set_status(job, "succeeded")
            return

        try:
            self.storage_dir.mkdir(parents=True, exist_ok=True)
            return_code = self._run_downloader(job)
            with self.lock:
                job.return_code = return_code
            if return_code != 0:
                raise RuntimeError(f"downloader exited with code {return_code}")
            self._package_cbz(job)
        except Exception as exc:  # noqa: BLE001 - keep job failures visible to clients.
            self._append_log(job, f"ERROR: {exc}")
            self._set_status(job, "failed", error=str(exc))
            return

        self._set_status(job, "succeeded")

    def _run_downloader(self, job: Job) -> int:
        if self.downloader_command is None:
            command = [str(self.script_path), job.gallery_id]
        else:
            command = [*self.downloader_command, job.gallery_id]

        self._append_log(job, f"Starting download for {job.gallery_id}")
        with subprocess.Popen(
            command,
            cwd=self.project_root,
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        ) as process:
            assert process.stdout is not None
            for line in process.stdout:
                self._append_log(job, line)
            return process.wait()

    def _package_cbz(self, job: Job) -> None:
        folder = self.folder_path(job.gallery_id)
        archive = self.archive_path(job.gallery_id)
        if archive.exists():
            self._append_log(job, f"{archive.name} already exists")
            return
        if not folder.is_dir():
            raise FileNotFoundError(f"download folder not found: {folder}")

        temp_archive = archive.with_name(f".{archive.name}.{job.job_id}.tmp")
        self._append_log(job, f"Packaging {archive.name}")
        try:
            with zipfile.ZipFile(temp_archive, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                for path in sorted(folder.rglob("*")):
                    if path.is_file():
                        zf.write(path, path.relative_to(folder))
            os.replace(temp_archive, archive)
            shutil.rmtree(folder)
            self._append_log(job, f"Created {archive}")
        except Exception:
            temp_archive.unlink(missing_ok=True)
            raise


def make_handler(
    manager: DownloadManager,
    allowed_networks: tuple[ipaddress._BaseNetwork, ...],
) -> type[BaseHTTPRequestHandler]:
    class NhRequestHandler(BaseHTTPRequestHandler):
        server_version = "NhDownloaderHTTP/1.0"

        def do_OPTIONS(self) -> None:
            if not self._is_allowed():
                self._send_json({"error": "forbidden"}, status=HTTPStatus.FORBIDDEN)
                return
            self._send_json({"ok": True})

        def do_GET(self) -> None:
            if not self._is_allowed():
                self._send_json({"error": "forbidden"}, status=HTTPStatus.FORBIDDEN)
                return

            path = urlparse(self.path).path
            if path == "/health":
                self._send_json({"ok": True})
                return
            if path.startswith("/api/jobs/"):
                job_id = path.removeprefix("/api/jobs/")
                job = manager.get_job(job_id)
                if job is None:
                    self._send_json({"error": "job not found"}, status=HTTPStatus.NOT_FOUND)
                    return
                self._send_json(job.to_dict())
                return
            if path.startswith("/api/galleries/"):
                gallery_id = path.removeprefix("/api/galleries/")
                if not is_valid_gallery_id(gallery_id):
                    self._send_json({"error": "id must be a string of digits"}, status=HTTPStatus.BAD_REQUEST)
                    return
                self._send_json(manager.gallery_status(gallery_id))
                return
            self._send_json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:
            if not self._is_allowed():
                self._send_json({"error": "forbidden"}, status=HTTPStatus.FORBIDDEN)
                return

            path = urlparse(self.path).path
            if path not in {"/api/download", "/api/galleries/status"}:
                self._send_json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)
                return

            try:
                payload = self._read_json()
            except ValueError as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return

            if path == "/api/galleries/status":
                gallery_ids = payload.get("ids")
                if not isinstance(gallery_ids, list) or not all(is_valid_gallery_id(item) for item in gallery_ids):
                    self._send_json({"error": "ids must be a list of digit strings"}, status=HTTPStatus.BAD_REQUEST)
                    return
                unique_ids = list(dict.fromkeys(gallery_ids))
                self._send_json({"galleries": manager.galleries_status(unique_ids)})
                return

            gallery_id = payload.get("id")
            if not is_valid_gallery_id(gallery_id):
                self._send_json({"error": "id must be a string of digits"}, status=HTTPStatus.BAD_REQUEST)
                return

            job, created = manager.submit(gallery_id)
            status = HTTPStatus.ACCEPTED if created else HTTPStatus.OK
            self._send_json(job.to_dict(), status=status)

        def _read_json(self) -> dict[str, object]:
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError as exc:
                raise ValueError("invalid Content-Length") from exc
            if length <= 0:
                raise ValueError("empty request body")
            body = self.rfile.read(length)
            try:
                payload = json.loads(body.decode("utf-8"))
            except json.JSONDecodeError as exc:
                raise ValueError("invalid JSON body") from exc
            if not isinstance(payload, dict):
                raise ValueError("JSON body must be an object")
            return payload

        def _is_allowed(self) -> bool:
            return is_ip_allowed(self.client_address[0], allowed_networks)

        def _send_json(self, payload: dict[str, object], status: HTTPStatus = HTTPStatus.OK) -> None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, fmt: str, *args: object) -> None:
            print(f"{self.address_string()} - {fmt % args}")

    return NhRequestHandler


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="LAN HTTP server for nh-project downloads")
    parser.add_argument("--host", default=os.environ.get("NH_SERVER_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("NH_SERVER_PORT", "8765")))
    parser.add_argument("--cookie-file", default=os.environ.get("NH_COOKIE_FILE", str(PROJECT_ROOT / "cookie.sh")))
    parser.add_argument("--download-script", default=os.environ.get("NH_DOWNLOAD_SCRIPT", str(PROJECT_ROOT / "nh2_requireCfToken.sh")))
    parser.add_argument("--storage-dir", default=os.environ.get("NH_FOLDER_PATH"))
    parser.add_argument(
        "--allowed-network",
        action="append",
        dest="allowed_networks",
        default=None,
        help="CIDR allowed to use the API. Repeatable.",
    )
    return parser


def main() -> None:
    args = build_arg_parser().parse_args()
    env = load_cookie_env(Path(args.cookie_file))
    if args.storage_dir:
        env["NH_FOLDER_PATH"] = args.storage_dir

    manager = DownloadManager(
        project_root=PROJECT_ROOT,
        script_path=Path(args.download_script),
        storage_dir=expand_path(env.get("NH_FOLDER_PATH", str(Path.home() / "nh"))),
        env=env,
    )
    networks = parse_networks(args.allowed_networks or DEFAULT_ALLOWED_NETWORKS)
    handler = make_handler(manager, networks)

    httpd = ThreadingHTTPServer((args.host, args.port), handler)
    print(f"nh downloader server listening on http://{args.host}:{args.port}")
    print("allowed networks:", ", ".join(str(network) for network in networks))
    httpd.serve_forever()


if __name__ == "__main__":
    main()
