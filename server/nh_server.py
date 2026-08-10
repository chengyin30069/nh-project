#!/usr/bin/env python3
"""LAN-only HTTP API for queueing nh-project downloads."""

from __future__ import annotations

import argparse
import hashlib
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
    "100.64.0.0/10",
)
TERMINAL_STATUSES = {"succeeded", "failed"}
GALLERY_ID_RE = re.compile(r"^[0-9]+$")
GALLERY_PAGE_RE = re.compile(r"^/g/([0-9]+)/?$")
GALLERY_READER_RE = re.compile(r"^/g/([0-9]+)/([0-9]+)/?$")
MEDIA_RE = re.compile(r"^/media/([0-9]+)/([^/]+)$")
LOCAL_API_PREFIX = "/_nh-local/api"
LOCAL_ASSET_PREFIX = "/_nh-local/assets"
MAX_JSON_BODY_BYTES = 64 * 1024
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
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

LOCAL_UI_CSS = r"""
.nh-local-overlay-target{position:relative!important;isolation:isolate!important}
.nh-local-card-controls{position:absolute!important;z-index:40!important;top:6px!important;right:6px!important;display:flex;gap:5px;align-items:center}
.nh-local-btn,.nh-local-badge{box-sizing:border-box;min-height:25px;border:0;border-radius:5px;padding:0 8px;color:#fff!important;font:700 12px/25px system-ui,sans-serif;box-shadow:0 2px 8px #0008;text-decoration:none!important}
.nh-local-btn{background:#ed2553!important;cursor:pointer}.nh-local-btn:hover{background:#ff4774!important}.nh-local-btn:disabled{cursor:default;opacity:.75}
.nh-local-delete{background:#9f3030!important}.nh-local-delete:hover{background:#c43d3d!important}.nh-local-badge{background:#238847!important;pointer-events:none}
.nh-local-detail-controls{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin:12px 0}.nh-local-detail-controls .nh-local-btn{font-size:14px;min-height:36px;line-height:36px;padding:0 14px}
.nh-local-error{color:#ff9c9c;font:13px/1.4 system-ui,sans-serif}
.nh-local-stale{position:sticky;top:0;z-index:2147483645;padding:7px 12px;background:#8a5a00;color:#fff;text-align:center;font:600 13px/1.3 system-ui,sans-serif}
.nh-local-modal{position:fixed;inset:0;z-index:2147483647;display:grid;place-items:center;padding:18px;background:#000a}
.nh-local-dialog{width:min(440px,100%);border-radius:8px;padding:18px;background:#202327;color:#f6f7f8;box-shadow:0 18px 46px #000b;font:14px/1.45 system-ui,sans-serif}
.nh-local-dialog h2{margin:0 0 10px;font-size:18px}.nh-local-dialog p{overflow-wrap:anywhere}.nh-local-actions{display:flex;justify-content:flex-end;gap:8px}.nh-local-actions button{min-height:34px;border:0;border-radius:4px;padding:0 12px;color:#fff;font-weight:700;cursor:pointer}.nh-local-cancel{background:#555d66}.nh-local-confirm{background:#b73535}
a[href^="/login"],a[href^="/register"],a[href^="/favorites"],a[href^="/upload"]{display:none!important}
iframe,.advertisement,.adsbyexoclick,.ad-container{display:none!important}
"""

LOCAL_UI_JS = r"""
(function(){
  "use strict";
  var API="/_nh-local/api";
  function idFrom(value){try{return new URL(value,location.href).pathname.match(/^\/g\/(\d+)\/?$/)?.[1]||null}catch(_){return null}}
  function clean(value){return(value||"").replace(/\s+/g," ").trim()}
  async function request(path,options){var response=await fetch(API+path,options);var body=await response.json().catch(function(){return{}});if(!response.ok)throw new Error(body.error||("HTTP "+response.status));return body}
  function button(label,kind){var item=document.createElement("button");item.type="button";item.className="nh-local-btn "+(kind||"");item.textContent=label;return item}
  function titleFor(card,id){return clean(card?.querySelector(".caption")?.textContent||card?.querySelector("img")?.alt||document.querySelector("#info h1")?.textContent)||("ID "+id)}
  function modal(id,title,onDone){var shade=document.createElement("div");shade.className="nh-local-modal";shade.innerHTML='<div class="nh-local-dialog" role="dialog" aria-modal="true"><h2>Delete downloaded gallery?</h2><p></p><div class="nh-local-error" hidden></div><div class="nh-local-actions"><button class="nh-local-cancel">Cancel</button><button class="nh-local-confirm">Delete</button></div></div>';shade.querySelector("p").textContent="ID "+id+" — "+title;var close=function(){shade.remove()};shade.querySelector(".nh-local-cancel").onclick=close;shade.onclick=function(e){if(e.target===shade)close()};shade.querySelector(".nh-local-confirm").onclick=async function(){var controls=shade.querySelectorAll("button");controls.forEach(function(x){x.disabled=true});try{await request("/galleries/"+id,{method:"DELETE"});close();onDone()}catch(error){var box=shade.querySelector(".nh-local-error");box.hidden=false;box.textContent=error.message;controls.forEach(function(x){x.disabled=false})}};document.body.appendChild(shade)}
  async function startDownload(id,control,refresh){control.disabled=true;control.textContent="Queued";try{var job=await request("/download",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({id:id})});while(job.status==="queued"||job.status==="running"){control.textContent=job.status==="running"?"Running":"Queued";await new Promise(function(resolve){setTimeout(resolve,1000)});job=await request("/jobs/"+job.job_id)}if(job.status!=="succeeded")throw new Error(job.error||"Download failed");refresh()}catch(error){control.disabled=false;control.textContent="Retry";control.title=error.message}}
  function render(target,id,status,title,detail){target.replaceChildren();if(status.downloaded){delete target.dataset.watch;var badge=document.createElement("span");badge.className="nh-local-badge";badge.textContent="Downloaded";var del=button("Delete","nh-local-delete");del.onclick=function(e){e.preventDefault();e.stopPropagation();modal(id,title,function(){render(target,id,{downloaded:false},title,detail)})};target.append(badge,del);return}var dl=button(detail?"Download":"DL");if(status.status==="queued"||status.status==="running"){dl.textContent=status.status==="running"?"Running":"Queued";dl.disabled=true;if(status.job_id&&target.dataset.watch!==status.job_id){target.dataset.watch=status.job_id;(async function(){try{var job=status;while(job.status==="queued"||job.status==="running"){await new Promise(function(resolve){setTimeout(resolve,1000)});job=await request("/jobs/"+status.job_id);dl.textContent=job.status==="running"?"Running":"Queued"}render(target,id,{downloaded:job.status==="succeeded",status:job.status,error:job.error},title,detail)}catch(error){dl.disabled=false;dl.textContent="Retry";dl.title=error.message}})()}}else if(status.status==="failed"){dl.textContent="Retry";dl.title=status.error||"Download failed"}dl.onclick=function(e){e.preventDefault();e.stopPropagation();startDownload(id,dl,function(){render(target,id,{downloaded:true},title,detail)})};target.appendChild(dl)}
  async function boot(){
    document.querySelectorAll("iframe,.advertisement,.adsbyexoclick,.ad-container").forEach(function(x){x.remove()});
    var cards=new Map();document.querySelectorAll('a[href*="/g/"]').forEach(function(link){var id=idFrom(link.href);if(!id||cards.has(id))return;var card=link.closest(".gallery")||link.closest(".thumb-container");if(!card)return;var cover=link.querySelector("img")?link:card;cover.classList.add("nh-local-overlay-target");var holder=document.createElement("span");holder.className="nh-local-card-controls";cover.appendChild(holder);cards.set(id,{holder:holder,title:titleFor(card,id)})});
    var detailId=location.pathname.match(/^\/g\/(\d+)\/?$/)?.[1];var detail=null;if(detailId){detail=document.createElement("div");detail.className="nh-local-detail-controls";var anchor=document.querySelector("#info")||document.querySelector("main")||document.body;anchor.appendChild(detail)}
    var ids=Array.from(cards.keys());if(detailId&&!ids.includes(detailId))ids.push(detailId);if(!ids.length)return;
    try{var result=await request("/galleries/status",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({ids:ids})});cards.forEach(function(value,id){render(value.holder,id,result.galleries[id]||{},value.title,false)});if(detail)render(detail,detailId,result.galleries[detailId]||{},titleFor(null,detailId),true)}catch(error){if(detail){detail.textContent="Local status unavailable: "+error.message;detail.classList.add("nh-local-error")}}
  }
  if(document.readyState==="loading")document.addEventListener("DOMContentLoaded",boot);else boot();
}());
"""


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
        self.gallery_locks: dict[str, threading.Lock] = {}
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
                "error": job.error if job else None,
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
        with self.gallery_lock(gallery_id):
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

    def gallery_lock(self, gallery_id: str) -> threading.Lock:
        with self.lock:
            return self.gallery_locks.setdefault(gallery_id, threading.Lock())

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


@dataclass(frozen=True)
class CacheSettings:
    html_ttl_seconds: int = 15 * 60
    html_max_age_seconds: int = 7 * 24 * 60 * 60
    html_max_bytes: int = 512 * 1024 * 1024
    extract_max_bytes: int = 5 * 1024 * 1024 * 1024
    sweep_interval_seconds: int = 60 * 60

    @classmethod
    def from_env(cls, env: dict[str, str]) -> "CacheSettings":
        names = {
            "html_ttl_seconds": "NH_HTML_CACHE_TTL_SECONDS",
            "html_max_age_seconds": "NH_HTML_CACHE_MAX_AGE_SECONDS",
            "html_max_bytes": "NH_HTML_CACHE_MAX_BYTES",
            "extract_max_bytes": "NH_EXTRACT_CACHE_MAX_BYTES",
            "sweep_interval_seconds": "NH_CACHE_SWEEP_INTERVAL_SECONDS",
        }
        values: dict[str, int] = {}
        defaults = cls()
        for field_name, env_name in names.items():
            raw = env.get(env_name)
            value = int(raw) if raw is not None else getattr(defaults, field_name)
            if value <= 0:
                raise ValueError(f"{env_name} must be greater than zero")
            values[field_name] = value
        return cls(**values)


class LocalCache:
    """Bounded HTML and extracted-image cache rooted inside the library."""

    def __init__(self, manager: DownloadManager, settings: CacheSettings, *, autostart: bool = True) -> None:
        self.manager = manager
        self.settings = settings
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.worker: threading.Thread | None = None
        if autostart:
            self.worker = threading.Thread(target=self._sweep_loop, name="nh-cache-sweeper", daemon=True)
            self.worker.start()

    def proxy_path(self, path_with_query: str) -> Path:
        digest = hashlib.sha256(path_with_query.encode("utf-8")).hexdigest()
        return self.manager.local_cache_root() / "proxy" / f"{digest}.html"

    def read_html(self, path: Path) -> tuple[str, bool] | None:
        try:
            stat = path.stat()
            source = path.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            return None
        now = time.time()
        try:
            os.utime(path, (now, stat.st_mtime))
        except FileNotFoundError:
            pass
        return source, now - stat.st_mtime <= self.settings.html_ttl_seconds

    def write_html(self, path: Path, source: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        temp.write_text(source, encoding="utf-8")
        os.replace(temp, path)
        self.cleanup()

    def touch_extract(self, extract_dir: Path) -> None:
        marker = extract_dir / ".complete"
        if marker.exists():
            try:
                marker.touch()
            except FileNotFoundError:
                pass

    def cleanup(self, *, protect_extract: str | None = None) -> None:
        if not self.lock.acquire(blocking=False):
            return
        try:
            self._cleanup_html()
            self._cleanup_extract(protect_extract=protect_extract)
        finally:
            self.lock.release()

    def _cleanup_html(self) -> None:
        root = self.manager.local_cache_root()
        files: list[tuple[float, int, Path]] = []
        now = time.time()
        for name in ("html", "metadata", "proxy"):
            directory = root / name
            if not directory.exists():
                continue
            for path in directory.rglob("*"):
                if not path.is_file() or path.name.startswith("."):
                    continue
                try:
                    stat = path.stat()
                except FileNotFoundError:
                    continue
                last_used = max(stat.st_atime, stat.st_mtime)
                if now - last_used > self.settings.html_max_age_seconds:
                    path.unlink(missing_ok=True)
                    continue
                files.append((last_used, stat.st_size, path))
        total = sum(item[1] for item in files)
        for _last_used, size, path in sorted(files):
            if total <= self.settings.html_max_bytes:
                break
            try:
                path.unlink()
                total -= size
            except FileNotFoundError:
                pass

    def _cleanup_extract(self, *, protect_extract: str | None = None) -> None:
        root = self.manager.local_cache_root() / "extract"
        if not root.exists():
            return
        entries: list[tuple[float, int, Path]] = []
        for directory in root.iterdir():
            if not directory.is_dir() or directory.name.startswith("."):
                continue
            marker = directory / ".complete"
            try:
                last_used = marker.stat().st_mtime
            except FileNotFoundError:
                last_used = directory.stat().st_mtime
            size = sum(path.stat().st_size for path in directory.rglob("*") if path.is_file())
            entries.append((last_used, size, directory))
        total = sum(item[1] for item in entries)
        for _last_used, size, directory in sorted(entries):
            if total <= self.settings.extract_max_bytes:
                break
            gallery_id = directory.name
            if gallery_id == protect_extract:
                continue
            lock = self.manager.gallery_lock(gallery_id)
            if not lock.acquire(blocking=False):
                continue
            try:
                if directory.exists():
                    shutil.rmtree(directory)
                    total -= size
            finally:
                lock.release()

    def _sweep_loop(self) -> None:
        while not self.stop_event.wait(self.settings.sweep_interval_seconds):
            self.cleanup()


class LocalLibrary:
    PUBLIC_PREFIXES = {
        "artist",
        "artists",
        "categories",
        "category",
        "character",
        "characters",
        "community",
        "group",
        "groups",
        "info",
        "language",
        "languages",
        "parodies",
        "parody",
        "random",
        "search",
        "tag",
        "tags",
        "users",
    }
    BLOCKED_PREFIXES = {"api", "favorites", "login", "register", "upload"}

    def __init__(
        self,
        manager: DownloadManager,
        *,
        env: dict[str, str] | None = None,
        cache_settings: CacheSettings | None = None,
        cache_autostart: bool = True,
    ) -> None:
        self.manager = manager
        self.env = dict(env or os.environ)
        self.cache = LocalCache(
            manager,
            cache_settings or CacheSettings.from_env(self.env),
            autostart=cache_autostart,
        )

    def gallery_html(self, gallery_id: str) -> str:
        cache_path = self.manager.local_html_dir(gallery_id) / "cover_page.html"
        try:
            source, stale = self._cached_html(cache_path, f"/g/{gallery_id}/")
        except Exception:
            if not self.manager.archive_path(gallery_id).exists():
                raise
            source = self._archive_cover_html(gallery_id) or self._fallback_gallery_html(gallery_id)
            stale = False
        self._cache_metadata(gallery_id, source)
        return self.rewrite_html(source, stale=stale)

    def reader_html(self, gallery_id: str, page: str) -> str:
        if not self.manager.archive_path(gallery_id).exists():
            return self._download_required_html(gallery_id)
        local_image = self.page_image_path(gallery_id, page)
        if local_image is None:
            return self._fallback_reader_html(gallery_id, page, None)
        try:
            source, stale = self._cached_html(
                self.manager.local_html_dir(gallery_id) / f"{page}.html",
                f"/g/{gallery_id}/{page}/",
            )
        except Exception:
            return self._fallback_reader_html(gallery_id, page, f"/media/{gallery_id}/{quote(local_image.name)}")

        local_src = f"/media/{gallery_id}/{quote(local_image.name)}"
        replaced = PAGE_IMAGE_RE.sub(rf'\g<prefix>{local_src}\g<suffix>', source, count=1)
        if replaced != source:
            return self.rewrite_html(replaced, stale=stale)
        return self._fallback_reader_html(gallery_id, page, local_src)

    def proxy_html(self, path_with_query: str) -> str:
        if not self.is_public_html_path(path_with_query):
            raise PermissionError("route is not available on the local gallery")
        source, stale = self._cached_html(self.cache.proxy_path(path_with_query), path_with_query)
        return self.rewrite_html(source, stale=stale)

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
            and path.suffix.lower() in IMAGE_SUFFIXES
        ]
        if candidates:
            self.cache.touch_extract(extract_dir)
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
                self.cache.touch_extract(extract_dir)
                return path
        return None

    def rewrite_html(self, source: str, *, stale: bool = False) -> str:
        source = re.sub(r"<meta\b[^>]*(?:delegate-ch|tsyndicate|exoclick)[^>]*>", "", source, flags=re.IGNORECASE)
        source = re.sub(
            r"<script\b[^>]*\bsrc=[\"'][^\"']*(?:cloudflareinsights|tsyndicate|exoclick|popads)[^\"']*[\"'][^>]*>\s*</script>",
            "",
            source,
            flags=re.IGNORECASE,
        )
        source = re.sub(r"<iframe\b[^>]*>.*?</iframe>", "", source, flags=re.IGNORECASE | re.DOTALL)
        source = re.sub(
            r"<a\b[^>]*\bhref=[\"'](?:https?:)?//[^\"']*(?:tsyndicate|exoclick|popads)[^\"']*[\"'][^>]*>.*?</a>",
            "",
            source,
            flags=re.IGNORECASE | re.DOTALL,
        )
        source = re.sub(
            r"<li\b[^>]*\bclass=[\"'][^\"']*(?:menu-sign-in|menu-register)[^\"']*[\"'][^>]*>.*?</li>",
            "",
            source,
            flags=re.IGNORECASE | re.DOTALL,
        )
        rewritten = ABS_NHENTAI_HREF_RE.sub(r"\g<prefix>\g<url>\g<suffix>", source)
        rewritten = INTERNAL_HREF_RE.sub(lambda match: f"{match.group('prefix')}{match.group('url')}{match.group('suffix')}", rewritten)
        rewritten = self._inject_local_navigation(rewritten)
        if stale:
            banner = '<div class="nh-local-stale">Upstream unavailable — showing cached content.</div>'
            rewritten = re.sub(r"(<body\b[^>]*>)", rf"\1{banner}", rewritten, count=1, flags=re.IGNORECASE)
        return rewritten

    def _inject_local_navigation(self, source: str) -> str:
        if "data-nh-local-navigation" in source:
            return source
        assets = (
            '<link data-nh-local-navigation="true" rel="stylesheet" href="/_nh-local/assets/local.css">'
            + LOCAL_NAVIGATION_SCRIPT.replace("<script>", '<script data-nh-local-navigation="true">', 1)
            + '<script defer src="/_nh-local/assets/local.js"></script>'
        )
        if "<head>" in source:
            return source.replace("<head>", f"<head>{assets}", 1)
        if "<head " in source:
            return re.sub(r"(<head\b[^>]*>)", rf"\1{assets}", source, count=1, flags=re.IGNORECASE)
        return f"{assets}{source}"

    def _cached_html(self, cache_path: Path, upstream_path: str) -> tuple[str, bool]:
        cached = self.cache.read_html(cache_path)
        if cached is not None and cached[1]:
            return cached[0], False
        try:
            source = self._fetch_nhentai_html(upstream_path)
        except Exception:
            if cached is not None:
                return cached[0], True
            raise
        self.cache.write_html(cache_path, source)
        return source, False

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

    def is_public_html_path(self, path_with_query: str) -> bool:
        path = urlparse(path_with_query).path
        if path == "/":
            return True
        first = path.strip("/").split("/", 1)[0].lower()
        return first in self.PUBLIC_PREFIXES and first not in self.BLOCKED_PREFIXES

    def _looks_like_html_path(self, path_with_query: str) -> bool:
        path = urlparse(path_with_query).path
        if path.startswith("/_app/"):
            return False
        suffix = Path(path).suffix.lower()
        return suffix in {"", ".html"}

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
        temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        temp.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp, path)
        self.cache.cleanup()

    def _ensure_extracted(self, gallery_id: str) -> Path | None:
        archive = self.manager.archive_path(gallery_id)
        if not archive.exists():
            return None
        with self.manager.gallery_lock(gallery_id):
            extract_dir = self.manager.local_extract_dir(gallery_id)
            marker = extract_dir / ".complete"
            if marker.exists():
                self.cache.touch_extract(extract_dir)
                return extract_dir
            temp_dir = extract_dir.with_name(f".{gallery_id}.{uuid.uuid4().hex}.tmp")
            temp_dir.mkdir(parents=True, exist_ok=True)
            try:
                with zipfile.ZipFile(archive) as zf:
                    members = [
                        member
                        for member in zf.infolist()
                        if not member.is_dir() and Path(member.filename).suffix.lower() in IMAGE_SUFFIXES
                    ]
                    max_uncompressed = max(archive.stat().st_size * 20, 1024 * 1024 * 1024)
                    if sum(member.file_size for member in members) > max_uncompressed:
                        raise ValueError("CBZ expanded size exceeds safety limit")
                    for member in members:
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
            except Exception:
                shutil.rmtree(temp_dir, ignore_errors=True)
                raise
        self.cache.cleanup(protect_extract=gallery_id)
        return extract_dir

    def _archive_cover_html(self, gallery_id: str) -> str | None:
        archive = self.manager.archive_path(gallery_id)
        if not archive.exists():
            return None
        try:
            with zipfile.ZipFile(archive) as zf:
                candidates = [name for name in zf.namelist() if Path(name).name == "cover_page.html"]
                if not candidates:
                    return None
                return zf.read(sorted(candidates)[0]).decode("utf-8", "replace")
        except (OSError, zipfile.BadZipFile, KeyError):
            return None

    def _gallery_images(self, gallery_id: str) -> list[Path]:
        extract_dir = self._ensure_extracted(gallery_id)
        if extract_dir is None:
            return []
        images = [
            path
            for path in extract_dir.rglob("*")
            if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES and path.stem.isdigit()
        ]

        def sort_key(path: Path) -> tuple[int, int | str, str]:
            return (0, int(path.stem), path.name) if path.stem.isdigit() else (1, path.stem, path.name)

        return sorted(images, key=sort_key)

    def _fallback_gallery_html(self, gallery_id: str) -> str:
        title = f"Gallery {gallery_id}"
        images = self._gallery_images(gallery_id)
        cover = f'<img src="/media/{gallery_id}/{quote(images[0].name)}" alt="{html.escape(title)}">' if images else ""
        return (
            "<!doctype html><html><head>"
            f"<title>{html.escape(title)}</title>"
            "<style>body{background:#111;color:#eee;font-family:sans-serif;max-width:1000px;margin:auto;padding:24px}"
            "a{color:#ff4774}img{max-width:320px;height:auto}</style></head><body>"
            f"<h1>{html.escape(title)}</h1>"
            f"{cover}<p>Local archive: {html.escape(self.manager.archive_path(gallery_id).name)} · {len(images)} pages</p>"
            f"<p><a href=\"/g/{gallery_id}/1/\">Open reader</a></p>"
            "</body></html>"
        )

    def _download_required_html(self, gallery_id: str) -> str:
        title = f"Gallery {gallery_id} is not downloaded"
        return self.rewrite_html(
            "<!doctype html><html><head>"
            f"<title>{html.escape(title)}</title>"
            "<style>body{margin:0;background:#111;color:#eee;text-align:center;font-family:sans-serif;padding:10vh 20px}"
            "a{color:#ff4774}</style></head><body>"
            f"<main id=\"info\"><h1>{html.escape(title)}</h1>"
            "<p>Full-size pages are available only after the CBZ has been downloaded.</p>"
            f"<p><a href=\"/g/{gallery_id}/\">Back to gallery</a></p></main></body></html>"
        )

    def _fallback_reader_html(self, gallery_id: str, page: str, image_src: str | None) -> str:
        title = f"Gallery {gallery_id} - page {page}"
        image = f'<img src="{html.escape(image_src)}" alt="{html.escape(title)}">' if image_src else "<p>Page image unavailable.</p>"
        page_count = len(self._gallery_images(gallery_id))
        next_page = str(min(int(page) + 1, page_count or int(page) + 1)) if page.isdigit() else page
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
            if not self._is_allowed() or not self._origin_allowed():
                self._send_json({"error": "forbidden"}, status=HTTPStatus.FORBIDDEN)
                return
            self._send_json({"ok": True})

        def do_GET(self) -> None:
            if not self._is_allowed() or not self._origin_allowed():
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
            if not self._is_allowed() or not self._origin_allowed():
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
                if not isinstance(gallery_ids, list) or len(gallery_ids) > 100 or not all(is_valid_gallery_id(item) for item in gallery_ids):
                    self._send_json({"error": "ids must be a list of at most 100 digit strings"}, status=HTTPStatus.BAD_REQUEST)
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
            if not self._is_allowed() or not self._origin_allowed():
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
            if length > MAX_JSON_BODY_BYTES:
                raise ValueError("request body is too large")
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

        def _origin_allowed(self) -> bool:
            origin = self.headers.get("Origin")
            return origin is None or origin.startswith("moz-extension://")

        def _send_json(self, payload: dict[str, object], status: HTTPStatus = HTTPStatus.OK) -> None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            origin = self.headers.get("Origin")
            if origin and self._origin_allowed():
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Vary", "Origin")
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

            if path == f"{LOCAL_ASSET_PREFIX}/local.css":
                self._send_bytes(LOCAL_UI_CSS.encode("utf-8"), "text/css; charset=utf-8")
                return
            if path == f"{LOCAL_ASSET_PREFIX}/local.js":
                self._send_bytes(LOCAL_UI_JS.encode("utf-8"), "text/javascript; charset=utf-8")
                return
            if path == f"{LOCAL_API_PREFIX}/queue":
                self._send_json(library.manager.queue_snapshot())
                return
            if path.startswith(f"{LOCAL_API_PREFIX}/jobs/"):
                job_id = path.removeprefix(f"{LOCAL_API_PREFIX}/jobs/")
                job = library.manager.get_job(job_id)
                if job is None:
                    self._send_json({"error": "job not found"}, status=HTTPStatus.NOT_FOUND)
                    return
                self._send_json(job.to_dict())
                return
            if path.startswith(f"{LOCAL_API_PREFIX}/galleries/"):
                gallery_id = path.removeprefix(f"{LOCAL_API_PREFIX}/galleries/")
                if not is_valid_gallery_id(gallery_id):
                    self._send_json({"error": "id must be a string of digits"}, status=HTTPStatus.BAD_REQUEST)
                    return
                self._send_json(library.manager.gallery_status(gallery_id))
                return

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
            except PermissionError as exc:
                self._send_text(str(exc), status=HTTPStatus.NOT_FOUND)
            except Exception as exc:  # noqa: BLE001 - surface proxy failures to the browser.
                self._send_text(f"upstream fetch failed: {exc}", status=HTTPStatus.BAD_GATEWAY)

        def do_POST(self) -> None:
            if not self._is_allowed():
                self._send_json({"error": "forbidden"}, status=HTTPStatus.FORBIDDEN)
                return
            path = urlparse(self.path).path
            if path not in {f"{LOCAL_API_PREFIX}/download", f"{LOCAL_API_PREFIX}/galleries/status"}:
                self._send_json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)
                return
            try:
                payload = self._read_json()
            except ValueError as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return
            if path.endswith("/galleries/status"):
                gallery_ids = payload.get("ids")
                if not isinstance(gallery_ids, list) or len(gallery_ids) > 100 or not all(is_valid_gallery_id(item) for item in gallery_ids):
                    self._send_json({"error": "ids must be a list of at most 100 digit strings"}, status=HTTPStatus.BAD_REQUEST)
                    return
                unique_ids = list(dict.fromkeys(gallery_ids))
                self._send_json({"galleries": library.manager.galleries_status(unique_ids)})
                return
            gallery_id = payload.get("id")
            if not is_valid_gallery_id(gallery_id):
                self._send_json({"error": "id must be a string of digits"}, status=HTTPStatus.BAD_REQUEST)
                return
            job, created = library.manager.submit(gallery_id)
            self._send_json(job.to_dict(), status=HTTPStatus.ACCEPTED if created else HTTPStatus.OK)

        def do_DELETE(self) -> None:
            if not self._is_allowed():
                self._send_json({"error": "forbidden"}, status=HTTPStatus.FORBIDDEN)
                return
            path = urlparse(self.path).path
            prefix = f"{LOCAL_API_PREFIX}/galleries/"
            if not path.startswith(prefix):
                self._send_json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)
                return
            gallery_id = path.removeprefix(prefix)
            if not is_valid_gallery_id(gallery_id):
                self._send_json({"error": "id must be a string of digits"}, status=HTTPStatus.BAD_REQUEST)
                return
            result = library.manager.delete_gallery(gallery_id)
            self._send_json(result, status=HTTPStatus.CONFLICT if result.get("blocked") else HTTPStatus.OK)

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

        def _read_json(self) -> dict[str, object]:
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError as exc:
                raise ValueError("invalid Content-Length") from exc
            if length <= 0:
                raise ValueError("empty request body")
            if length > MAX_JSON_BODY_BYTES:
                raise ValueError("request body is too large")
            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
            except json.JSONDecodeError as exc:
                raise ValueError("invalid JSON body") from exc
            if not isinstance(payload, dict):
                raise ValueError("JSON body must be an object")
            return payload

        def _send_json(self, payload: dict[str, object], status: HTTPStatus = HTTPStatus.OK) -> None:
            self._send_bytes(json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8", status=status)

        def _send_bytes(self, data: bytes, content_type: str, status: HTTPStatus = HTTPStatus.OK) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            if content_type.lower().startswith("text/html"):
                self.send_header(
                    "Content-Security-Policy",
                    "default-src 'self'; img-src 'self' data: blob: https://*.nhentai.net; "
                    "script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline' https://cdnjs.cloudflare.com; "
                    "font-src 'self' https://cdnjs.cloudflare.com; connect-src 'self'; frame-src 'none'; object-src 'none'",
                )
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
    parser.add_argument("--html-cache-ttl", type=int, help="HTML freshness lifetime in seconds")
    parser.add_argument("--html-cache-max-age", type=int, help="remove HTML cache entries unused for this many seconds")
    parser.add_argument("--html-cache-max-bytes", type=int, help="maximum combined HTML and metadata cache size")
    parser.add_argument("--extract-cache-max-bytes", type=int, help="maximum extracted image cache size")
    parser.add_argument("--cache-sweep-interval", type=int, help="background cache cleanup interval in seconds")
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
    cache_args = {
        "NH_HTML_CACHE_TTL_SECONDS": args.html_cache_ttl,
        "NH_HTML_CACHE_MAX_AGE_SECONDS": args.html_cache_max_age,
        "NH_HTML_CACHE_MAX_BYTES": args.html_cache_max_bytes,
        "NH_EXTRACT_CACHE_MAX_BYTES": args.extract_cache_max_bytes,
        "NH_CACHE_SWEEP_INTERVAL_SECONDS": args.cache_sweep_interval,
    }
    for name, value in cache_args.items():
        if value is not None:
            env[name] = str(value)

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
