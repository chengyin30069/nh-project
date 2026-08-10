#!/usr/bin/env python3
"""LAN-only HTTP API for queueing nh-project downloads."""

from __future__ import annotations

import argparse
import html
import ipaddress
import json
import mimetypes
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
from typing import Iterable
from urllib.parse import quote, unquote, urlparse
from urllib.request import Request, urlopen


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ALLOWED_NETWORKS = (
    "192.168.50.0/24",
    "192.168.193.0/24",
    "127.0.0.1/32",
    "::1/128",
    "100.0.0.0/8",
)
TERMINAL_STATUSES = {"succeeded", "failed"}
GALLERY_ID_RE = re.compile(r"^[0-9]+$")
GALLERY_PAGE_RE = re.compile(r"^/g/([0-9]+)/?$")
GALLERY_READER_RE = re.compile(r"^/g/([0-9]+)/([0-9]+)/?$")
MEDIA_RE = re.compile(r"^/media/([0-9]+)/([^/]+)$")
WINDOW_GALLERY_RE = re.compile(r"window\._gallery\s*=\s*JSON\.parse\((?P<value>\"(?:\\.|[^\"\\])*\")\)")
INTERNAL_HREF_RE = re.compile(r'(?P<prefix>\s(?:href|action)=["\'])(?P<url>/(?!/)[^"\']*)(?P<suffix>["\'])', re.IGNORECASE)
ABS_NHENTAI_HREF_RE = re.compile(
    r'(?P<prefix>\s(?:href|action)=["\'])https://nhentai\.net(?P<url>/[^"\']*)(?P<suffix>["\'])',
    re.IGNORECASE,
)
PAGE_IMAGE_RE = re.compile(
    r'(?P<prefix><img\b[^>]*\bsrc=["\'])https://i[0-9]*\.nhentai\.net/galleries/[0-9]+/[0-9]+\.(?:jpg|jpeg|png|gif|webp)(?P<suffix>["\'][^>]*>)',
    re.IGNORECASE,
)
LOCAL_NAVIGATION_SCRIPT = """<script>
(function () {
  function isLocalHttpUrl(url) {
    return url.origin === window.location.origin && /^(http|https):$/.test(url.protocol);
  }
  document.addEventListener("click", function (event) {
    var anchor = event.target && event.target.closest ? event.target.closest("a[href]") : null;
    if (!anchor || anchor.target || anchor.hasAttribute("download")) {
      return;
    }
    var url = new URL(anchor.getAttribute("href"), window.location.href);
    if (!isLocalHttpUrl(url)) {
      return;
    }
    event.preventDefault();
    event.stopImmediatePropagation();
    window.location.assign(url.href);
  }, true);
  document.addEventListener("submit", function (event) {
    var form = event.target;
    if (!form || !form.action) {
      return;
    }
    var url = new URL(form.action, window.location.href);
    if (!isLocalHttpUrl(url)) {
      return;
    }
    event.preventDefault();
    event.stopImmediatePropagation();
    var method = (form.method || "get").toLowerCase();
    if (method !== "get") {
      form.submit();
      return;
    }
    var params = new URLSearchParams(new FormData(form));
    url.search = params.toString();
    window.location.assign(url.href);
  }, true);
}());
</script>"""


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
        self.queued_job_ids: deque[str] = deque()
        self.recent_job_ids: deque[str] = deque(maxlen=30)
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
                self.recent_job_ids.appendleft(job.job_id)
                return job, True

            self.jobs[job.job_id] = job
            self.active_by_gallery_id[gallery_id] = job.job_id
            self.queued_job_ids.append(job.job_id)
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

    def queue_snapshot(self) -> dict[str, object]:
        with self.lock:
            running = [job.to_dict() for job in self.jobs.values() if job.status == "running"]
            queued = [
                self.jobs[job_id].to_dict()
                for job_id in self.queued_job_ids
                if job_id in self.jobs and self.jobs[job_id].status == "queued"
            ]
            recent = [
                self.jobs[job_id].to_dict()
                for job_id in self.recent_job_ids
                if job_id in self.jobs and self.jobs[job_id].status in TERMINAL_STATUSES
            ]
            return {
                "running": running,
                "queued": queued,
                "recent": recent,
                "counts": {
                    "running": len(running),
                    "queued": len(queued),
                    "recent": len(recent),
                },
            }

    def delete_gallery(self, gallery_id: str) -> dict[str, object]:
        if not is_valid_gallery_id(gallery_id):
            raise ValueError("gallery id must contain digits only")

        with self.lock:
            job_id = self.active_by_gallery_id.get(gallery_id)
            job = self.jobs.get(job_id) if job_id else None
            if job and job.status not in TERMINAL_STATUSES:
                return {
                    "id": gallery_id,
                    "deleted": False,
                    "blocked": True,
                    "status": job.status,
                    "error": "gallery has an active download job",
                }

        deleted_paths: list[str] = []
        for path in (
            self.archive_path(gallery_id),
            self.folder_path(gallery_id),
            self.local_html_dir(gallery_id),
            self.local_metadata_path(gallery_id),
            self.local_extract_dir(gallery_id),
        ):
            if path.is_dir():
                shutil.rmtree(path)
                deleted_paths.append(str(path))
            elif path.exists():
                path.unlink()
                deleted_paths.append(str(path))

        with self.lock:
            if self.active_by_gallery_id.get(gallery_id) == job_id:
                self.active_by_gallery_id.pop(gallery_id, None)

        return {
            "id": gallery_id,
            "deleted": bool(deleted_paths),
            "blocked": False,
            "deleted_paths": deleted_paths,
        }

    def archive_path(self, gallery_id: str) -> Path:
        return self.storage_dir / f"{gallery_id}.cbz"

    def folder_path(self, gallery_id: str) -> Path:
        return self.storage_dir / gallery_id

    def local_cache_root(self) -> Path:
        return self.storage_dir / ".nh-local"

    def local_html_dir(self, gallery_id: str) -> Path:
        return self.local_cache_root() / "html" / gallery_id

    def local_metadata_path(self, gallery_id: str) -> Path:
        return self.local_cache_root() / "metadata" / f"{gallery_id}.json"

    def local_extract_dir(self, gallery_id: str) -> Path:
        return self.local_cache_root() / "extract" / gallery_id

    def _worker_loop(self) -> None:
        while True:
            job = self.queue.get()
            try:
                self._mark_dequeued(job)
                self._run_job(job)
            finally:
                self.queue.task_done()

    def _mark_dequeued(self, job: Job) -> None:
        with self.lock:
            try:
                self.queued_job_ids.remove(job.job_id)
            except ValueError:
                pass

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
                if job.job_id in self.recent_job_ids:
                    self.recent_job_ids.remove(job.job_id)
                self.recent_job_ids.appendleft(job.job_id)
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


class LocalLibrary:
    def __init__(self, manager: DownloadManager, *, env: dict[str, str] | None = None) -> None:
        self.manager = manager
        self.env = dict(env or os.environ)

    def gallery_html(self, gallery_id: str) -> str:
        cache_path = self.manager.local_html_dir(gallery_id) / "cover_page.html"
        if cache_path.exists():
            source = cache_path.read_text(encoding="utf-8", errors="replace")
            if not self.manager.local_metadata_path(gallery_id).exists():
                self._cache_metadata(gallery_id, source)
        else:
            try:
                source = self._fetch_nhentai_html(f"/g/{gallery_id}/")
            except Exception:
                source = self._fallback_gallery_html(gallery_id)
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(source, encoding="utf-8")
            self._cache_metadata(gallery_id, source)
        return self.rewrite_html(source)

    def reader_html(self, gallery_id: str, page: str) -> str:
        local_image = self.page_image_path(gallery_id, page) if self.manager.archive_path(gallery_id).exists() else None
        source = self._cached_reader_html(gallery_id, page)
        if local_image is None:
            return self.rewrite_html(source)

        local_src = f"/media/{gallery_id}/{quote(local_image.name)}"
        replaced = PAGE_IMAGE_RE.sub(rf'\g<prefix>{local_src}\g<suffix>', source, count=1)
        if replaced != source:
            return self.rewrite_html(replaced)
        return self._fallback_reader_html(gallery_id, page, local_src)

    def proxy_html(self, path_with_query: str) -> str:
        cache_path = self.manager.local_cache_root() / "proxy" / f"{quote(path_with_query, safe='')}.html"
        if cache_path.exists():
            source = cache_path.read_text(encoding="utf-8", errors="replace")
        else:
            source = self._fetch_nhentai_html(path_with_query)
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(source, encoding="utf-8")
        return self.rewrite_html(source)

    def proxy_response(self, path_with_query: str) -> tuple[bytes, str]:
        if self._looks_like_html_path(path_with_query):
            return self.proxy_html(path_with_query).encode("utf-8"), "text/html; charset=utf-8"
        data, content_type, _ = self._fetch_nhentai(path_with_query)
        return data, content_type

    def page_image_path(self, gallery_id: str, page: str) -> Path | None:
        extract_dir = self._ensure_extracted(gallery_id)
        if extract_dir is None:
            return None
        candidates = [
            path
            for path in extract_dir.rglob("*")
            if path.is_file()
            and path.stem == page
            and path.suffix.lower() in {".jpg", ".jpeg", ".png", ".gif", ".webp"}
        ]
        return sorted(candidates)[0] if candidates else None

    def media_path(self, gallery_id: str, filename: str) -> Path | None:
        extract_dir = self._ensure_extracted(gallery_id)
        if extract_dir is None:
            return None
        for path in extract_dir.rglob(filename):
            if path.is_file() and path.name == filename:
                try:
                    path.resolve().relative_to(extract_dir.resolve())
                except ValueError:
                    continue
                return path
        return None

    def rewrite_html(self, source: str) -> str:
        rewritten = ABS_NHENTAI_HREF_RE.sub(r"\g<prefix>\g<url>\g<suffix>", source)
        rewritten = INTERNAL_HREF_RE.sub(lambda match: f"{match.group('prefix')}{match.group('url')}{match.group('suffix')}", rewritten)
        return self._inject_local_navigation(rewritten)

    def _inject_local_navigation(self, source: str) -> str:
        if "data-nh-local-navigation" in source:
            return source
        script = LOCAL_NAVIGATION_SCRIPT.replace("<script>", '<script data-nh-local-navigation="true">', 1)
        if "<head>" in source:
            return source.replace("<head>", f"<head>{script}", 1)
        if "<head " in source:
            return re.sub(r"(<head\b[^>]*>)", rf"\1{script}", source, count=1, flags=re.IGNORECASE)
        return f"{script}{source}"

    def _cached_reader_html(self, gallery_id: str, page: str) -> str:
        cache_path = self.manager.local_html_dir(gallery_id) / f"{page}.html"
        if cache_path.exists():
            return cache_path.read_text(encoding="utf-8", errors="replace")
        try:
            source = self._fetch_nhentai_html(f"/g/{gallery_id}/{page}/")
        except Exception:
            source = self._fallback_reader_html(gallery_id, page, None)
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(source, encoding="utf-8")
        return source

    def _fetch_nhentai_html(self, path_with_query: str) -> str:
        data, _content_type, charset = self._fetch_nhentai(path_with_query)
        return data.decode(charset, "replace")

    def _fetch_nhentai(self, path_with_query: str) -> tuple[bytes, str, str]:
        headers = {}
        if self.env.get("NH_COOKIE"):
            headers["Cookie"] = self.env["NH_COOKIE"]
        if self.env.get("NH_USER_AGENT"):
            headers["User-Agent"] = self.env["NH_USER_AGENT"]
        request = Request(f"https://nhentai.net{path_with_query}", headers=headers)
        with urlopen(request, timeout=20) as response:  # noqa: S310 - user-configured local proxy target.
            charset = response.headers.get_content_charset() or "utf-8"
            content_type = response.headers.get("Content-Type", "application/octet-stream")
            return response.read(), content_type, charset

    def _looks_like_html_path(self, path_with_query: str) -> bool:
        path = urlparse(path_with_query).path
        suffix = Path(path).suffix.lower()
        return suffix not in {
            ".css",
            ".js",
            ".mjs",
            ".png",
            ".jpg",
            ".jpeg",
            ".gif",
            ".webp",
            ".svg",
            ".ico",
            ".woff",
            ".woff2",
            ".ttf",
            ".map",
        }

    def _cache_metadata(self, gallery_id: str, source: str) -> None:
        match = WINDOW_GALLERY_RE.search(source)
        if not match:
            return
        try:
            metadata = json.loads(json.loads(match.group("value")))
        except json.JSONDecodeError:
            return
        path = self.manager.local_metadata_path(gallery_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")

    def _ensure_extracted(self, gallery_id: str) -> Path | None:
        archive = self.manager.archive_path(gallery_id)
        if not archive.exists():
            return None
        extract_dir = self.manager.local_extract_dir(gallery_id)
        marker = extract_dir / ".complete"
        if marker.exists():
            return extract_dir
        temp_dir = extract_dir.with_name(f".{gallery_id}.{uuid.uuid4().hex}.tmp")
        temp_dir.mkdir(parents=True, exist_ok=True)
        try:
            with zipfile.ZipFile(archive) as zf:
                for member in zf.infolist():
                    if member.is_dir():
                        continue
                    target = temp_dir / member.filename
                    try:
                        target.resolve().relative_to(temp_dir.resolve())
                    except ValueError:
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(member) as source, target.open("wb") as output:
                        shutil.copyfileobj(source, output)
            (temp_dir / ".complete").write_text("ok\n", encoding="utf-8")
            if extract_dir.exists():
                shutil.rmtree(extract_dir)
            os.replace(temp_dir, extract_dir)
            return extract_dir
        except Exception:
            shutil.rmtree(temp_dir, ignore_errors=True)
            raise

    def _fallback_gallery_html(self, gallery_id: str) -> str:
        title = f"Gallery {gallery_id}"
        return (
            "<!doctype html><html><head>"
            f"<title>{html.escape(title)}</title>"
            "</head><body>"
            f"<h1>{html.escape(title)}</h1>"
            f"<p>Local archive: {html.escape(self.manager.archive_path(gallery_id).name)}</p>"
            f"<p><a href=\"/g/{gallery_id}/1/\">Open reader</a></p>"
            "</body></html>"
        )

    def _fallback_reader_html(self, gallery_id: str, page: str, image_src: str | None) -> str:
        title = f"Gallery {gallery_id} - page {page}"
        image = f'<img src="{html.escape(image_src)}" alt="{html.escape(title)}">' if image_src else "<p>Page image unavailable.</p>"
        next_page = str(int(page) + 1) if page.isdigit() else page
        prev_page = str(max(int(page) - 1, 1)) if page.isdigit() else page
        return (
            "<!doctype html><html><head>"
            f"<title>{html.escape(title)}</title>"
            "<style>body{margin:0;background:#111;color:#eee;text-align:center;font-family:sans-serif}"
            "nav{padding:12px}a{color:#8cc8ff;margin:0 12px}img{max-width:100%;height:auto}</style>"
            "</head><body>"
            f"<nav><a href=\"/g/{gallery_id}/\">Gallery</a><a href=\"/g/{gallery_id}/{prev_page}/\">Prev</a>"
            f"<a href=\"/g/{gallery_id}/{next_page}/\">Next</a></nav>{image}</body></html>"
        )


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
            if path == "/api/queue":
                self._send_json(manager.queue_snapshot())
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

        def do_DELETE(self) -> None:
            if not self._is_allowed():
                self._send_json({"error": "forbidden"}, status=HTTPStatus.FORBIDDEN)
                return

            path = urlparse(self.path).path
            if not path.startswith("/api/galleries/"):
                self._send_json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)
                return

            gallery_id = path.removeprefix("/api/galleries/")
            if not is_valid_gallery_id(gallery_id):
                self._send_json({"error": "id must be a string of digits"}, status=HTTPStatus.BAD_REQUEST)
                return

            result = manager.delete_gallery(gallery_id)
            if result.get("blocked"):
                self._send_json(result, status=HTTPStatus.CONFLICT)
                return
            self._send_json(result)

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
            self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, fmt: str, *args: object) -> None:
            print(f"{self.address_string()} - {fmt % args}")

    return NhRequestHandler


def make_library_handler(
    library: LocalLibrary,
    allowed_networks: tuple[ipaddress._BaseNetwork, ...],
) -> type[BaseHTTPRequestHandler]:
    class NhLibraryRequestHandler(BaseHTTPRequestHandler):
        server_version = "NhLocalLibraryHTTP/1.0"

        def do_OPTIONS(self) -> None:
            if not self._is_allowed():
                self._send_text("forbidden", status=HTTPStatus.FORBIDDEN)
                return
            self._send_text("ok")

        def do_GET(self) -> None:
            if not self._is_allowed():
                self._send_text("forbidden", status=HTTPStatus.FORBIDDEN)
                return

            parsed = urlparse(self.path)
            path = parsed.path
            query = f"?{parsed.query}" if parsed.query else ""

            media_match = MEDIA_RE.fullmatch(path)
            if media_match:
                gallery_id, filename = media_match.groups()
                media_path = library.media_path(gallery_id, unquote(filename))
                if media_path is None:
                    self._send_text("not found", status=HTTPStatus.NOT_FOUND)
                    return
                self._send_file(media_path)
                return

            gallery_match = GALLERY_PAGE_RE.fullmatch(path)
            if gallery_match:
                self._send_html(library.gallery_html(gallery_match.group(1)))
                return

            reader_match = GALLERY_READER_RE.fullmatch(path)
            if reader_match:
                gallery_id, page = reader_match.groups()
                self._send_html(library.reader_html(gallery_id, page))
                return

            if path.startswith("/api/") or path.startswith("/media/"):
                self._send_text("not found", status=HTTPStatus.NOT_FOUND)
                return

            try:
                data, content_type = library.proxy_response(f"{path}{query}")
                self._send_bytes(data, content_type)
            except Exception as exc:  # noqa: BLE001 - surface proxy failures to the browser.
                self._send_text(f"upstream fetch failed: {exc}", status=HTTPStatus.BAD_GATEWAY)

        def _is_allowed(self) -> bool:
            return is_ip_allowed(self.client_address[0], allowed_networks)

        def _send_html(self, payload: str, status: HTTPStatus = HTTPStatus.OK) -> None:
            self._send_bytes(payload.encode("utf-8"), "text/html; charset=utf-8", status=status)

        def _send_text(self, payload: str, status: HTTPStatus = HTTPStatus.OK) -> None:
            self._send_bytes(payload.encode("utf-8"), "text/plain; charset=utf-8", status=status)

        def _send_file(self, path: Path) -> None:
            content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            with path.open("rb") as source:
                data = source.read()
            self._send_bytes(data, content_type)

        def _send_bytes(self, data: bytes, content_type: str, status: HTTPStatus = HTTPStatus.OK) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, fmt: str, *args: object) -> None:
            print(f"{self.address_string()} - {fmt % args}")

    return NhLibraryRequestHandler


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="LAN HTTP server for nh-project downloads")
    parser.add_argument("--host", default=os.environ.get("NH_SERVER_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("NH_SERVER_PORT", "8765")))
    parser.add_argument("--library-host", default=os.environ.get("NH_LIBRARY_HOST"))
    parser.add_argument("--library-port", type=int, default=int(os.environ.get("NH_LIBRARY_PORT", "8766")))
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
    library = LocalLibrary(manager, env=env)
    library_handler = make_library_handler(library, networks)

    httpd = ThreadingHTTPServer((args.host, args.port), handler)
    library_host = args.library_host or args.host
    library_httpd = ThreadingHTTPServer((library_host, args.library_port), library_handler)
    library_thread = threading.Thread(target=library_httpd.serve_forever, name="nh-local-library-server", daemon=True)
    library_thread.start()
    print(f"nh downloader server listening on http://{args.host}:{args.port}")
    print(f"nh local library server listening on http://{library_host}:{args.library_port}")
    print("allowed networks:", ", ".join(str(network) for network in networks))
    try:
        httpd.serve_forever()
    finally:
        library_httpd.shutdown()
        library_httpd.server_close()


if __name__ == "__main__":
    main()
