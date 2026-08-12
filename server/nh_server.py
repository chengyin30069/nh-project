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
import random
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
from urllib.parse import parse_qs, quote, unquote, urlparse
from urllib.error import HTTPError
from urllib.request import Request, urlopen

try:
    from server.library_db import GALLERY_TYPES, LibraryDatabase, LibraryIndexer, parse_gallery_metadata
except ModuleNotFoundError:  # Direct execution: python3 server/nh_server.py
    from library_db import GALLERY_TYPES, LibraryDatabase, LibraryIndexer, parse_gallery_metadata


PROJECT_ROOT = Path(__file__).resolve().parents[1]
LOCAL_UI_CSS_PATH = PROJECT_ROOT / "server" / "static" / "local-ui.css"
LOCAL_UI_JS_PATH = PROJECT_ROOT / "server" / "static" / "local-ui.js"
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
LOCAL_GALLERY_PAGE_RE = re.compile(r"^/downloads/g/([0-9]+)/?$")
LOCAL_GALLERY_READER_RE = re.compile(r"^/downloads/g/([0-9]+)/([0-9]+)/?$")
LOCAL_TAXONOMY_RE = re.compile(
    r"^/downloads/(tag|artist|character|parody|group|language|category)/([^/]+)/?$"
)
MEDIA_RE = re.compile(r"^/media/([0-9]+)/([^/]+)$")
PREVIEW_MEDIA_RE = re.compile(r"^/preview-media/([0-9]+)/([0-9]+)$")
PREVIEW_THUMB_RE = re.compile(r"^/preview-thumbnail/([0-9]+)/([0-9]+)$")
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
IMAGE_INDEX_FILENAME = ".images.json"
ARCHIVE_COVER_RE = re.compile(r'<meta\s+itemprop=["\']image["\']\s+content=["\'](?P<url>(?://|https://)[^"\']+)["\']', re.IGNORECASE)
ARCHIVE_TITLE_RE = re.compile(r'<meta\s+itemprop=["\']name["\']\s+content=["\'](?P<title>[^"\']+)["\']', re.IGNORECASE)
LOCAL_NAVIGATION_SCRIPT = """<script>
(function () {
  function isLocalHttpUrl(url) {
    return url.origin === window.location.origin && /^(http|https):$/.test(url.protocol);
  }
  document.addEventListener("click", function (event) {
    var interactive = event.target && event.target.closest
      ? event.target.closest("button, input, select, textarea, [role='button'], #nh-delete-modal")
      : null;
    if (interactive) {
      return;
    }
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
        self.archive_ready_callback = None
        self.gallery_deleted_callback = None
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
                if self.archive_ready_callback is not None:
                    try:
                        self.archive_ready_callback(archive_path)
                    except Exception as exc:
                        job.logs.append(f"Metadata pending: {exc}")
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
                self.local_preview_dir(gallery_id),
            ):
                if path.is_dir():
                    shutil.rmtree(path)
                    deleted_paths.append(str(path))
                elif path.exists():
                    path.unlink()
                    deleted_paths.append(str(path))
            if self.gallery_deleted_callback is not None:
                self.gallery_deleted_callback(gallery_id)

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

    def local_preview_dir(self, gallery_id: str) -> Path:
        return self.local_cache_root() / "preview" / gallery_id

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
            if self.archive_ready_callback is not None:
                try:
                    self.archive_ready_callback(archive)
                except Exception as exc:  # Metadata repair is independent from the completed download.
                    self._append_log(job, f"Metadata pending: {exc}")
        except Exception:
            temp_archive.unlink(missing_ok=True)
            raise


@dataclass(frozen=True)
class CacheSettings:
    html_ttl_seconds: int = 15 * 60
    api_ttl_seconds: int = 60
    html_max_age_seconds: int = 7 * 24 * 60 * 60
    html_max_bytes: int = 512 * 1024 * 1024
    extract_max_bytes: int = 5 * 1024 * 1024 * 1024
    preview_max_age_seconds: int = 24 * 60 * 60
    preview_max_bytes: int = 2 * 1024 * 1024 * 1024
    sweep_interval_seconds: int = 60 * 60

    @classmethod
    def from_env(cls, env: dict[str, str]) -> "CacheSettings":
        names = {
            "html_ttl_seconds": "NH_HTML_CACHE_TTL_SECONDS",
            "api_ttl_seconds": "NH_API_CACHE_TTL_SECONDS",
            "html_max_age_seconds": "NH_HTML_CACHE_MAX_AGE_SECONDS",
            "html_max_bytes": "NH_HTML_CACHE_MAX_BYTES",
            "extract_max_bytes": "NH_EXTRACT_CACHE_MAX_BYTES",
            "preview_max_age_seconds": "NH_PREVIEW_CACHE_MAX_AGE_SECONDS",
            "preview_max_bytes": "NH_PREVIEW_CACHE_MAX_BYTES",
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

    def api_path(self, path_with_query: str) -> Path:
        digest = hashlib.sha256(path_with_query.encode("utf-8")).hexdigest()
        return self.manager.local_cache_root() / "proxy" / f"api-{digest}.json"

    def read_bytes(self, path: Path, ttl_seconds: int) -> tuple[bytes, bool] | None:
        try:
            stat = path.stat()
            data = path.read_bytes()
        except FileNotFoundError:
            return None
        now = time.time()
        try:
            os.utime(path, (now, stat.st_mtime))
        except FileNotFoundError:
            pass
        return data, now - stat.st_mtime <= ttl_seconds

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
        self.write_bytes(path, source.encode("utf-8"))

    def write_bytes(self, path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        temp.write_bytes(data)
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
            self._cleanup_preview()
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

    def _cleanup_preview(self) -> None:
        root = self.manager.local_cache_root() / "preview"
        if not root.exists():
            return
        now = time.time()
        files: list[tuple[float, int, Path]] = []
        for path in root.rglob("*"):
            if not path.is_file() or path.name.startswith("."):
                continue
            try:
                stat = path.stat()
            except FileNotFoundError:
                continue
            last_used = max(stat.st_atime, stat.st_mtime)
            if now - last_used > self.settings.preview_max_age_seconds:
                path.unlink(missing_ok=True)
                continue
            files.append((last_used, stat.st_size, path))
        total = sum(item[1] for item in files)
        for _last_used, size, path in sorted(files):
            if total <= self.settings.preview_max_bytes:
                break
            try:
                path.unlink()
                total -= size
            except FileNotFoundError:
                pass
        for directory in sorted((path for path in root.rglob("*") if path.is_dir()), reverse=True):
            try:
                directory.rmdir()
            except OSError:
                pass

    def _sweep_loop(self) -> None:
        while not self.stop_event.wait(self.settings.sweep_interval_seconds):
            self.cleanup()


@dataclass(frozen=True)
class UpstreamResponse:
    data: bytes
    content_type: str
    charset: str
    status: int
    final_path: str


@dataclass(frozen=True)
class ProxyResponse:
    data: bytes
    content_type: str
    status: int = HTTPStatus.OK
    stale: bool = False


class LocalRedirect(Exception):
    def __init__(self, location: str, *, no_store: bool = False) -> None:
        super().__init__(location)
        self.location = location
        self.no_store = no_store


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
        "taxonomy",
        "users",
    }
    BLOCKED_PREFIXES = {"api", "favorites", "login", "register", "upload"}
    PUBLIC_API_PATTERNS = (
        re.compile(r"^/api/v2/(?:cdn|config)$"),
        re.compile(r"^/api/v2/galleries(?:/(?:popular|random|tagged|[0-9]+(?:/(?:comments|related|suggestions))?))?$"),
        re.compile(r"^/api/v2/gts/(?:backlog|new-tags)$"),
        re.compile(r"^/api/v2/search$"),
        re.compile(r"^/api/v2/tags/(?:search|(?:tag|artist|parody|character|group|language|category)(?:/[^/]+)?)$"),
        re.compile(r"^/api/v2/taxonomy(?:/(?:resolved|stats|[0-9]+(?:/(?:comments|edits))?))?$"),
        re.compile(r"^/api/v2/users/[0-9]+/[^/]+$"),
    )

    def __init__(
        self,
        manager: DownloadManager,
        *,
        env: dict[str, str] | None = None,
        cache_settings: CacheSettings | None = None,
        cache_autostart: bool = True,
        index_autostart: bool | None = None,
    ) -> None:
        self.manager = manager
        self.env = dict(env or os.environ)
        self.cache = LocalCache(
            manager,
            cache_settings or CacheSettings.from_env(self.env),
            autostart=cache_autostart,
        )
        self._image_indexes: dict[str, tuple[int, tuple[Path, ...]]] = {}
        self._image_index_lock = threading.Lock()
        self._catalog_records: dict[str, tuple[int, dict[str, object]]] = {}
        self._catalog_lock = threading.Lock()
        self._last_random_ids: tuple[str, ...] = ()
        self._random_lock = threading.Lock()
        self.database = LibraryDatabase(manager.storage_dir)
        self.indexer = LibraryIndexer(
            self.database,
            self._fetch_index_metadata,
            autostart=cache_autostart if index_autostart is None else index_autostart,
        )
        self.manager.archive_ready_callback = self._archive_ready
        self.manager.gallery_deleted_callback = self.database.delete_gallery

    def _archive_ready(self, archive: Path) -> None:
        status = self.database.index_archive(archive)
        if status == "pending":
            self.indexer.wake()

    def _fetch_index_metadata(self, gallery_id: str) -> dict[str, object]:
        response = self.public_api_response(f"/api/v2/galleries/{gallery_id}")
        if response.status < 200 or response.status >= 300:
            raise RuntimeError(f"upstream metadata returned HTTP {response.status}")
        metadata = json.loads(response.data)
        if not isinstance(metadata, dict) or str(metadata.get("id")) != gallery_id:
            raise ValueError("upstream gallery metadata is invalid")
        return metadata

    def _index_for_synchronous_use(self) -> None:
        if self.indexer.worker is None:
            self.indexer.index_now(repair_remote=False)

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

    def local_gallery_html(self, gallery_id: str) -> str | None:
        self._index_for_synchronous_use()
        record = self.database.gallery(gallery_id)
        if record is None or not self.manager.archive_path(gallery_id).exists():
            return None
        images = self._gallery_images(gallery_id)
        cover = (
            f'<img class="nh-local-gallery-cover" src="/media/{gallery_id}/{quote(images[0].name)}" '
            f'alt="{html.escape(str(record["title"]))}">' if images else
            '<div class="nh-catalog-placeholder">No cover</div>'
        )
        detail_metadata: dict[str, object] = {}
        try:
            detail_metadata = self._detail_preview_metadata(gallery_id)
        except Exception as exc:  # The local archive and reader must remain usable offline.
            print(f"preview metadata unavailable for {gallery_id}: {exc}")
        metadata_counts = {
            (str(tag.get("type")), str(tag.get("slug"))): int(tag["count"])
            for tag in detail_metadata.get("tags", [])
            if isinstance(tag, dict)
            and str(tag.get("type")) in GALLERY_TYPES
            and isinstance(tag.get("slug"), str)
            and str(tag.get("count", "")).isdigit()
        } if isinstance(detail_metadata.get("tags"), list) else {}
        groups: dict[str, list[str]] = {kind: [] for kind in GALLERY_TYPES}
        for tag in record.get("tags", []):
            if not isinstance(tag, dict) or str(tag.get("type")) not in groups:
                continue
            kind = str(tag["type"])
            upstream = str(tag.get("upstream_url") or f'/{kind}/{tag.get("slug", "")}/')
            slug = str(tag.get("slug") or "")
            local = f'/downloads/{kind}/{quote(slug)}/'
            local_count = int(tag.get("local_count") or 0)
            stored_upstream_count = tag.get("upstream_count")
            upstream_count = int(stored_upstream_count) if isinstance(stored_upstream_count, int) else -1
            if upstream_count < 0:
                upstream_count = metadata_counts.get((kind, slug), -1)
            groups[kind].append(
                f'<a class="nh-taxonomy-link" data-upstream-href="{html.escape(upstream)}" '
                f'data-local-href="{html.escape(local)}" href="{html.escape(local)}">'
                f'{html.escape(str(tag.get("name") or ""))}'
                f'<span class="nh-taxonomy-count" data-upstream-count="{upstream_count}" '
                f'data-local-count="{local_count}">{local_count}</span></a>'
            )
        rows = "".join(
            f'<div class="nh-local-tag-row"><strong>{kind.title()}:</strong><div>{"".join(items)}</div></div>'
            for kind, items in groups.items() if items
        )
        pending = (
            '<p class="nh-local-index-pending">Metadata indexing is still pending; some fields may be missing.</p>'
            if record.get("metadata_status") == "pending" else ""
        )
        preview_cards = ""
        if detail_metadata:
            pages = detail_metadata.get("pages") if isinstance(detail_metadata.get("pages"), list) else []
            preview_cards = "".join(
                f'<a class="nh-content-thumbnail" href="/downloads/g/{gallery_id}/{number}/">'
                f'<img loading="lazy" src="/preview-thumbnail/{gallery_id}/{number}" alt="Page {number}"></a>'
                for item in pages if isinstance(item, dict)
                for number in [item.get("number")]
                if isinstance(number, int) and number > 0
            )
        title = html.escape(str(record["title"]))
        secondary_title = html.escape(str(record.get("secondary_title") or ""))
        subtitle = f'<h2>{secondary_title}</h2>' if secondary_title and secondary_title != title else ""
        return (
            '<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{title}</title><link rel="stylesheet" href="/_nh-local/assets/local.css">'
            '<script defer src="/_nh-local/assets/local.js"></script></head><body class="nh-catalog-body">'
            f'{self._local_menu_html()}{self._catalog_site_header_html()}'
            '<main class="nh-local-gallery-page"><section class="nh-local-gallery-panel">'
            f'<div class="nh-local-gallery-cover-wrap">{cover}</div><div id="info" class="nh-local-gallery-info">'
            f'<h1>{title}</h1>{subtitle}<p class="nh-local-gallery-id">#{gallery_id}</p>{pending}'
            '<div class="nh-taxonomy-mode"><span>Tag links:</span><button type="button" data-nh-taxonomy-toggle>Local</button></div>'
            f'<div class="nh-local-tags">{rows}</div><p><a class="nh-local-reader-button" href="/downloads/g/{gallery_id}/1/">Read locally</a></p>'
            '</div></section>'
            f'<section class="nh-content-preview"><h2>Preview</h2><div class="nh-content-preview-grid">{preview_cards}</div></section>'
            '</main></body></html>'
        )

    def reader_html(self, gallery_id: str, page: str) -> str:
        if self.manager.archive_path(gallery_id).exists():
            images = self._gallery_images(gallery_id)
            local_image = next((path for path in images if path.stem == page), None)
            return self._local_reader_html(gallery_id, page, images, local_image, local=False)
        try:
            metadata = self._preview_metadata(gallery_id)
            pages = metadata.get("pages", [])
            if not isinstance(pages, list):
                pages = []
            page_number = int(page)
            image_src = f"/preview-media/{gallery_id}/{page_number}" if 1 <= page_number <= len(pages) else None
            next_page = min(page_number + 1, len(pages)) if pages else page_number
            next_src = f"/preview-media/{gallery_id}/{next_page}" if next_page != page_number else None
            return self._reader_shell_html(gallery_id, page_number, len(pages), image_src, next_src, preview=True)
        except Exception as exc:
            return self._preview_unavailable_html(gallery_id, str(exc))

    def local_reader_html(self, gallery_id: str, page: str) -> str | None:
        if not self.manager.archive_path(gallery_id).exists():
            return None
        images = self._gallery_images(gallery_id)
        local_image = next((path for path in images if path.stem == page), None)
        return self._local_reader_html(gallery_id, page, images, local_image, local=True)

    def downloaded_galleries_html(self, page: int) -> str:
        self._index_for_synchronous_use()
        records, total = self.database.downloaded(page=page)
        page_count = max(1, (total + 24) // 25)
        page = max(1, min(page, page_count))
        if page > 1 and not records:
            records, _total = self.database.downloaded(page=page)
        return self._catalog_page_html(
            "Recently Downloaded", records, page=page, page_count=page_count,
            base_path="/downloads/", show_page_jump=True,
        )

    def random_downloaded_html(self) -> str:
        self._index_for_synchronous_use()
        records = self.database.random(5)
        with self._random_lock:
            current_ids = tuple(str(record["id"]) for record in records)
            if current_ids == self._last_random_ids and len(records) == 5:
                records = self.database.random(5)
                current_ids = tuple(str(record["id"]) for record in records)
            self._last_random_ids = current_ids
        return self._catalog_page_html("Random 5 Downloads", records)

    def local_search_html(self, query: str, page: int) -> str:
        self._index_for_synchronous_use()
        requested_page = page
        records, total = self.database.search(query, page=requested_page)
        page_count = max(1, (total + 24) // 25)
        page = max(1, min(requested_page, page_count))
        if page != requested_page:
            records, _total = self.database.search(query, page=page)
        title = f'Search: "{query}"' if query else "All Downloads"
        base = f"/downloads/search/?q={quote(query)}"
        return self._catalog_page_html(title, records, page=page, page_count=page_count, base_path=base, search_query=query)

    def local_taxonomy_html(self, taxonomy_type: str, slug: str, page: int) -> str | None:
        self._index_for_synchronous_use()
        requested_page = page
        name, records, total = self.database.taxonomy(taxonomy_type, slug, page=requested_page)
        if name is None:
            return None
        page_count = max(1, (total + 24) // 25)
        page = max(1, min(requested_page, page_count))
        if page != requested_page:
            name, records, total = self.database.taxonomy(taxonomy_type, slug, page=page)
        return self._catalog_page_html(
            f"{taxonomy_type.title()}: {name}", records, page=page, page_count=page_count,
            base_path=f"/downloads/{taxonomy_type}/{quote(slug)}/",
        )

    def preview_media_path(self, gallery_id: str, page: str) -> Path | None:
        if self.manager.archive_path(gallery_id).exists():
            return self.page_image_path(gallery_id, page)
        metadata = self._preview_metadata(gallery_id)
        pages = metadata.get("pages")
        if not isinstance(pages, list):
            return None
        page_number = int(page)
        if page_number < 1 or page_number > len(pages) or not isinstance(pages[page_number - 1], dict):
            return None
        remote_path = pages[page_number - 1].get("path")
        if not isinstance(remote_path, str) or not remote_path.startswith("galleries/"):
            return None
        suffix = Path(remote_path).suffix.lower()
        if suffix not in IMAGE_SUFFIXES:
            return None
        target = self.manager.local_preview_dir(gallery_id) / f"{page_number}{suffix}"
        try:
            if target.is_file():
                target.touch()
                return target
        except FileNotFoundError:
            pass
        with self.manager.gallery_lock(gallery_id):
            if target.is_file():
                target.touch()
                return target
            data = self._fetch_cdn_image(remote_path, page_number)
            target.parent.mkdir(parents=True, exist_ok=True)
            temp = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
            temp.write_bytes(data)
            os.replace(temp, target)
        self.cache.cleanup()
        return target

    def preview_thumbnail_path(self, gallery_id: str, page: str) -> Path | None:
        metadata = self._detail_preview_metadata(gallery_id)
        pages = metadata.get("pages")
        if not isinstance(pages, list):
            return None
        page_number = int(page)
        if page_number < 1 or page_number > len(pages) or not isinstance(pages[page_number - 1], dict):
            return None
        item = pages[page_number - 1]
        remote_path = item.get("thumbnail")
        if not isinstance(remote_path, str) or not remote_path.startswith("galleries/"):
            full_path = item.get("path")
            if not isinstance(full_path, str) or not full_path.startswith("galleries/"):
                return None
            full = Path(full_path)
            remote_path = f"{full.parent.as_posix()}/{full.stem}t{full.suffix}"
        suffix = Path(remote_path).suffix.lower()
        if suffix not in IMAGE_SUFFIXES:
            return None
        target = self.manager.local_preview_dir(gallery_id) / "thumbnails" / f"{page_number}{suffix}"
        try:
            if target.is_file():
                target.touch()
                return target
        except FileNotFoundError:
            pass
        with self.manager.gallery_lock(f"preview-thumbnail-{gallery_id}-{page_number}"):
            if target.is_file():
                target.touch()
                return target
            data = self._fetch_cdn_thumbnail(remote_path, page_number)
            target.parent.mkdir(parents=True, exist_ok=True)
            temp = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
            temp.write_bytes(data)
            os.replace(temp, target)
        self.cache.cleanup()
        return target

    def proxy_html(self, path_with_query: str) -> str:
        if not self.is_public_html_path(path_with_query):
            raise PermissionError("route is not available on the local gallery")
        source, stale = self._cached_proxy_html(path_with_query)
        return self.rewrite_html(source, stale=stale)

    def proxy_response(self, path_with_query: str) -> ProxyResponse:
        if self._looks_like_html_path(path_with_query):
            return ProxyResponse(self.proxy_html(path_with_query).encode("utf-8"), "text/html; charset=utf-8")
        response = self._fetch_nhentai(path_with_query)
        return ProxyResponse(response.data, response.content_type, response.status)

    def public_api_response(self, path_with_query: str) -> ProxyResponse:
        path = urlparse(path_with_query).path
        if path == "/api/v2/zones" or path.startswith("/api/v2/zones/"):
            return ProxyResponse(b'{"zones":{}}', "application/json; charset=utf-8")
        if not self.is_public_api_path(path):
            raise PermissionError("API route is not available on the local gallery")

        cache_path = self.cache.api_path(path_with_query)
        cached = self.cache.read_bytes(cache_path, self.cache.settings.api_ttl_seconds)
        if cached is not None and cached[1]:
            return ProxyResponse(cached[0], "application/json; charset=utf-8")
        try:
            response = self._fetch_nhentai(path_with_query)
        except Exception:
            if cached is not None:
                return ProxyResponse(cached[0], "application/json; charset=utf-8", stale=True)
            raise
        if 200 <= response.status < 300:
            self.cache.write_bytes(cache_path, response.data)
            return ProxyResponse(response.data, response.content_type, response.status)
        if response.status >= 500 and cached is not None:
            return ProxyResponse(cached[0], "application/json; charset=utf-8", stale=True)
        return ProxyResponse(response.data, response.content_type, response.status)

    def page_image_path(self, gallery_id: str, page: str) -> Path | None:
        return next((path for path in self._gallery_images(gallery_id) if path.stem == page), None)

    def media_path(self, gallery_id: str, filename: str) -> Path | None:
        return next((path for path in self._gallery_images(gallery_id) if path.name == filename), None)

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
        rewritten = re.sub(r'(?P<quote>["\'])(?:(?:\.\./)|(?:\./))+_app/', r'\g<quote>/_app/', rewritten)
        rewritten = self._inject_local_navigation(rewritten)
        rewritten = self._inject_local_menu(rewritten)
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

    def _local_menu_html(self) -> str:
        return (
            '<nav class="nh-local-menu" data-nh-local-menu="true">'
            '<a href="/">Home</a><a href="/downloads/">Downloads</a>'
            '<a href="/downloads/random/">Random 5</a></nav>'
        )

    def _inject_local_menu(self, source: str) -> str:
        if "data-nh-local-menu" in source:
            return source
        menu = self._local_menu_html()
        if re.search(r"<body\b", source, flags=re.IGNORECASE):
            return re.sub(r"(<body\b[^>]*>)", rf"\1{menu}", source, count=1, flags=re.IGNORECASE)
        return f"{menu}{source}"

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

    def _cached_proxy_html(self, path_with_query: str) -> tuple[str, bool]:
        cache_path = self.cache.proxy_path(path_with_query)
        is_random = urlparse(path_with_query).path.rstrip("/") == "/random"
        cached = None if is_random else self.cache.read_html(cache_path)
        if cached is not None and cached[1]:
            return cached[0], False
        try:
            response = self._fetch_nhentai(path_with_query)
            if response.final_path != self._normalized_path(path_with_query):
                raise LocalRedirect(response.final_path, no_store=is_random)
            if response.status >= 400:
                raise RuntimeError(f"upstream returned HTTP {response.status}")
            source = response.data.decode(response.charset, "replace")
        except LocalRedirect:
            raise
        except Exception:
            if cached is not None:
                return cached[0], True
            raise
        self.cache.write_html(cache_path, source)
        return source, False

    def _fetch_nhentai_html(self, path_with_query: str) -> str:
        response = self._fetch_nhentai(path_with_query)
        if response.status >= 400:
            raise RuntimeError(f"upstream returned HTTP {response.status}")
        return response.data.decode(response.charset, "replace")

    def _fetch_nhentai(self, path_with_query: str) -> UpstreamResponse:
        headers = {}
        if self.env.get("NH_COOKIE"):
            headers["Cookie"] = self.env["NH_COOKIE"]
        if self.env.get("NH_USER_AGENT"):
            headers["User-Agent"] = self.env["NH_USER_AGENT"]
        request = Request(f"https://nhentai.net{path_with_query}", headers=headers)
        try:
            response = urlopen(request, timeout=20)  # noqa: S310 - fixed upstream host.
        except HTTPError as exc:
            response = exc
        with response:
            final_url = urlparse(response.geturl())
            if final_url.scheme != "https" or final_url.hostname != "nhentai.net":
                raise PermissionError("upstream redirected outside nhentai.net")
            charset = response.headers.get_content_charset() or "utf-8"
            content_type = response.headers.get("Content-Type", "application/octet-stream")
            final_path = final_url.path or "/"
            if final_url.query:
                final_path = f"{final_path}?{final_url.query}"
            return UpstreamResponse(response.read(), content_type, charset, response.status, final_path)

    def _normalized_path(self, path_with_query: str) -> str:
        parsed = urlparse(path_with_query)
        path = parsed.path or "/"
        return f"{path}?{parsed.query}" if parsed.query else path

    def is_public_html_path(self, path_with_query: str) -> bool:
        path = urlparse(path_with_query).path
        if path == "/":
            return True
        first = path.strip("/").split("/", 1)[0].lower()
        return first in self.PUBLIC_PREFIXES and first not in self.BLOCKED_PREFIXES

    def is_public_api_path(self, path: str) -> bool:
        return any(pattern.fullmatch(path) for pattern in self.PUBLIC_API_PATTERNS)

    def _looks_like_html_path(self, path_with_query: str) -> bool:
        path = urlparse(path_with_query).path
        if path.startswith("/_app/"):
            return False
        suffix = Path(path).suffix.lower()
        return suffix in {"", ".html"}

    def _cache_metadata(self, gallery_id: str, source: str) -> None:
        metadata = parse_gallery_metadata(source, gallery_id)
        if metadata is None:
            return
        path = self.manager.local_metadata_path(gallery_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        temp.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp, path)
        self.cache.cleanup()

    def _preview_metadata(self, gallery_id: str) -> dict[str, object]:
        path = self.manager.local_preview_dir(gallery_id) / "metadata.json"
        try:
            metadata = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(metadata, dict) and str(metadata.get("id")) == gallery_id:
                path.touch()
                return metadata
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            pass
        response = self.public_api_response(f"/api/v2/galleries/{gallery_id}")
        if response.status < 200 or response.status >= 300:
            raise RuntimeError(f"upstream metadata returned HTTP {response.status}")
        metadata = json.loads(response.data)
        if not isinstance(metadata, dict) or str(metadata.get("id")) != gallery_id or not isinstance(metadata.get("pages"), list):
            raise ValueError("upstream gallery metadata is invalid")
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        temp.write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")
        os.replace(temp, path)
        self.cache.cleanup()
        return metadata

    def _detail_preview_metadata(self, gallery_id: str) -> dict[str, object]:
        source = self._archive_cover_html(gallery_id) or ""
        metadata = parse_gallery_metadata(source, gallery_id)
        if isinstance(metadata, dict) and isinstance(metadata.get("pages"), list):
            return metadata
        if isinstance(metadata, dict):
            images = metadata.get("images")
            legacy_pages = images.get("pages") if isinstance(images, dict) else None
            media_id = str(metadata.get("media_id") or "")
            if isinstance(legacy_pages, list) and media_id.isdigit():
                extensions = {"j": ".jpg", "p": ".png", "g": ".gif", "w": ".webp"}
                pages = []
                for number, item in enumerate(legacy_pages, 1):
                    if not isinstance(item, dict) or item.get("t") not in extensions:
                        continue
                    suffix = extensions[str(item["t"])]
                    pages.append({
                        "number": number,
                        "thumbnail": f"galleries/{media_id}/{number}t{suffix}",
                    })
                if pages:
                    return {**metadata, "pages": pages}
        return self._preview_metadata(gallery_id)

    def _fetch_cdn_image(self, remote_path: str, page_number: int) -> bytes:
        return self._fetch_cdn_asset(remote_path, page_number, subdomain="i", max_bytes=100 * 1024 * 1024)

    def _fetch_cdn_thumbnail(self, remote_path: str, page_number: int) -> bytes:
        return self._fetch_cdn_asset(remote_path, page_number, subdomain="t", max_bytes=20 * 1024 * 1024)

    def _fetch_cdn_asset(self, remote_path: str, page_number: int, *, subdomain: str, max_bytes: int) -> bytes:
        configured = self.env.get("NH_MEDIA_SERVER_LIST", "1 2 3 4 5 6 7 8 9").split()
        servers = [item for item in configured if item.isdigit() and 1 <= int(item) <= 9]
        if not servers:
            servers = ["1", "2", "3", "4"]
        offset = (page_number - 1) % len(servers)
        servers = servers[offset:] + servers[:offset]
        headers = {}
        if self.env.get("NH_USER_AGENT"):
            headers["User-Agent"] = self.env["NH_USER_AGENT"]
        last_error: Exception | None = None
        safe_path = quote(remote_path.lstrip("/"), safe="/._-")
        for server in servers:
            request = Request(f"https://{subdomain}{server}.nhentai.net/{safe_path}", headers=headers)
            try:
                with urlopen(request, timeout=20) as response:  # noqa: S310 - validated nhentai CDN host.
                    final = urlparse(response.geturl())
                    if final.scheme != "https" or not re.fullmatch(rf"{subdomain}[1-9]\.nhentai\.net", final.hostname or ""):
                        raise PermissionError("image CDN redirected outside nhentai.net")
                    content_type = response.headers.get("Content-Type", "").lower()
                    if not content_type.startswith("image/"):
                        raise ValueError("preview response is not an image")
                    data = response.read(max_bytes + 1)
                    if len(data) > max_bytes:
                        raise ValueError("preview image exceeds size limit")
                    return data
            except Exception as exc:  # noqa: BLE001 - retry the configured CDN mirrors.
                last_error = exc
        raise RuntimeError(f"all preview image servers failed: {last_error}")

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
                self._write_image_index(temp_dir, self._scan_gallery_images(temp_dir))
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

    def _downloaded_archives(self) -> list[Path]:
        archives = [path for path in self.manager.storage_dir.glob("*.cbz") if is_valid_gallery_id(path.stem)]
        return sorted(archives, key=lambda path: (path.stat().st_mtime_ns, int(path.stem)), reverse=True)

    def _archive_catalog_record(self, archive: Path) -> dict[str, object]:
        stamp = archive.stat().st_mtime_ns
        with self._catalog_lock:
            cached = self._catalog_records.get(archive.stem)
        if cached is not None and cached[0] == stamp:
            return cached[1]
        source = self._archive_cover_html(archive.stem) or ""
        metadata: dict[str, object] = {}
        match = WINDOW_GALLERY_RE.search(source)
        if match:
            try:
                parsed = json.loads(json.loads(match.group("value")))
                if isinstance(parsed, dict):
                    metadata = parsed
            except json.JSONDecodeError:
                pass
        title_data = metadata.get("title")
        title = f"Gallery {archive.stem}"
        if isinstance(title_data, dict):
            title = str(title_data.get("english") or title_data.get("pretty") or title)
        elif title_match := ARCHIVE_TITLE_RE.search(source):
            title = html.unescape(title_match.group("title"))
        cover_match = ARCHIVE_COVER_RE.search(source)
        cover_url = cover_match.group("url") if cover_match else ""
        if cover_url.startswith("//"):
            cover_url = f"https:{cover_url}"
        record: dict[str, object] = {
            "id": archive.stem,
            "title": title,
            "cover_url": cover_url,
            "downloaded_at": archive.stat().st_mtime,
        }
        with self._catalog_lock:
            self._catalog_records[archive.stem] = (stamp, record)
        return record

    def _catalog_page_html(
        self,
        title: str,
        records: list[dict[str, object]],
        *,
        page: int | None = None,
        page_count: int | None = None,
        base_path: str = "/downloads/",
        search_query: str = "",
        show_page_jump: bool = False,
    ) -> str:
        cards = []
        for record in records:
            gallery_id = html.escape(str(record["id"]))
            gallery_title = html.escape(str(record["title"]))
            cover_url = html.escape(str(record.get("cover_url") or ""))
            image = f'<img loading="lazy" src="{cover_url}" alt="{gallery_title}">' if cover_url else '<div class="nh-catalog-placeholder">No cover</div>'
            cards.append(
                f'<div class="gallery"><a class="cover" href="/downloads/g/{gallery_id}/">{image}'
                f'<div class="caption">{gallery_title}</div></a></div>'
            )
        if not cards:
            cards.append('<p class="nh-catalog-empty">No downloaded galleries found.</p>')
        pagination = ""
        if page is not None and page_count is not None:
            links = []
            separator = "&amp;" if "?" in base_path else "?"
            if page > 1:
                links.append(f'<a href="{html.escape(base_path)}{separator}page={page - 1}">Prev</a>')
            links.append(f"<span>Page {page} / {page_count}</span>")
            if page < page_count:
                links.append(f'<a href="{html.escape(base_path)}{separator}page={page + 1}">Next</a>')
            if show_page_jump:
                links.append(
                    f'<form class="nh-page-jump" action="{html.escape(base_path)}" method="get">'
                    f'<label>Page <input type="number" name="page" min="1" max="{page_count}" '
                    f'value="{page}" inputmode="numeric" aria-label="Page number"></label>'
                    '<button type="submit">Go</button></form>'
                )
            pagination = f'<nav class="nh-catalog-pagination">{"".join(links)}</nav>'
        return (
            "<!doctype html><html><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            f"<title>{html.escape(title)}</title>"
            '<link rel="stylesheet" href="/_nh-local/assets/local.css"><script defer src="/_nh-local/assets/local.js"></script>'
            "</head><body class=\"nh-catalog-body\">"
            f"{self._local_menu_html()}{self._catalog_site_header_html(search_query)}"
            f'<main class="nh-catalog-page"><section class="nh-catalog-panel"><h1><span aria-hidden="true">▰</span> {html.escape(title)}</h1>'
            f'<div class="nh-catalog-grid">{"".join(cards)}</div>{pagination}</section></main></body></html>'
        )

    def _catalog_site_header_html(self, query: str = "") -> str:
        return (
            '<header class="nh-catalog-site-header"><a class="nh-catalog-logo" href="/" aria-label="Home">'
            '<img src="/logo.svg" alt="nhentai"></a>'
            '<form class="nh-catalog-search" action="/downloads/search/" method="get">'
            f'<input type="search" name="q" value="{html.escape(query)}" aria-label="Search local downloads" autocomplete="off">'
            '<button type="submit" aria-label="Search">⌕</button></form>'
            '<nav class="nh-catalog-primary-nav"><a href="/random/">Random</a><a href="/tags/">Tags</a>'
            '<a href="/artists/">Artists</a><a href="/characters/">Characters</a>'
            '<a href="/parodies/">Parodies</a><a href="/groups/">Groups</a>'
            '<a href="/community/taxonomy/">Community</a></nav></header>'
        )

    def _gallery_images(self, gallery_id: str) -> list[Path]:
        extract_dir = self._ensure_extracted(gallery_id)
        if extract_dir is None:
            return []
        index_path = extract_dir / IMAGE_INDEX_FILENAME
        try:
            index_stamp = index_path.stat().st_mtime_ns
        except FileNotFoundError:
            index_stamp = None
        with self._image_index_lock:
            cached = self._image_indexes.get(gallery_id)
        if cached is not None and cached[0] == index_stamp:
            return list(cached[1])

        images = self._read_image_index(extract_dir, index_path)
        if images is None:
            with self.manager.gallery_lock(gallery_id):
                images = self._read_image_index(extract_dir, index_path)
                if images is None:
                    images = self._scan_gallery_images(extract_dir)
                    self._write_image_index(extract_dir, images)
        index_stamp = index_path.stat().st_mtime_ns
        with self._image_index_lock:
            self._image_indexes[gallery_id] = (index_stamp, tuple(images))
        return images

    def _scan_gallery_images(self, extract_dir: Path) -> list[Path]:
        images = [
            path
            for path in extract_dir.rglob("*")
            if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES and path.stem.isdigit()
        ]
        return sorted(images, key=lambda path: (int(path.stem), path.name, path.as_posix()))

    def _read_image_index(self, extract_dir: Path, index_path: Path) -> list[Path] | None:
        try:
            entries = json.loads(index_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return None
        if not isinstance(entries, list) or not all(isinstance(entry, str) for entry in entries):
            return None
        images: list[Path] = []
        for entry in entries:
            path = extract_dir / entry
            try:
                path.resolve().relative_to(extract_dir.resolve())
            except ValueError:
                return None
            if not path.is_file() or path.suffix.lower() not in IMAGE_SUFFIXES:
                return None
            images.append(path)
        return images

    def _write_image_index(self, extract_dir: Path, images: list[Path]) -> None:
        index_path = extract_dir / IMAGE_INDEX_FILENAME
        entries = [path.relative_to(extract_dir).as_posix() for path in images]
        temp = index_path.with_name(f".{index_path.name}.{uuid.uuid4().hex}.tmp")
        temp.write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")
        os.replace(temp, index_path)

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

    def _local_reader_html(
        self, gallery_id: str, page: str, images: list[Path], image_path: Path | None, *, local: bool
    ) -> str:
        image_src = f"/media/{gallery_id}/{quote(image_path.name)}" if image_path else None
        page_count = len(images)
        page_number = int(page)
        next_page = min(page_number + 1, page_count or page_number + 1)
        next_image = next((path for path in images if path.stem == str(next_page)), None)
        next_src = f"/media/{gallery_id}/{quote(next_image.name)}" if next_image and next_page != page_number else None
        return self._reader_shell_html(
            gallery_id, page_number, page_count, image_src, next_src, preview=False,
            route_prefix="/downloads/g" if local else "/g",
        )

    def _reader_shell_html(
        self,
        gallery_id: str,
        page_number: int,
        page_count: int,
        image_src: str | None,
        next_image_src: str | None,
        *,
        preview: bool,
        route_prefix: str = "/g",
    ) -> str:
        title = f"Gallery {gallery_id} - page {page_number}"
        next_page = min(page_number + 1, page_count or page_number + 1)
        prev_page = max(page_number - 1, 1)
        image = f'<img src="{html.escape(image_src)}" alt="{html.escape(title)}">' if image_src else "<p>Page image unavailable.</p>"
        preload = f'<link rel="preload" as="image" href="{html.escape(next_image_src)}">' if next_image_src else ""
        preview_label = '<span class="nh-preview-label">Temporary preview</span>' if preview else ""
        return (
            "<!doctype html><html><head>"
            f'<meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(title)}</title>{preload}'
            "<style>body{margin:0;background:#111;color:#eee;text-align:center;font-family:sans-serif}"
            "nav{position:sticky;top:0;z-index:2;padding:12px;background:#111e}a{color:#8cc8ff;margin:0 12px}"
            "img{display:block;max-width:100%;height:auto;margin:auto}.nh-preview-label{color:#f5b942;margin-left:12px}</style>"
            "</head><body>"
            f"{self._local_menu_html()}<nav><a href=\"{route_prefix}/{gallery_id}/\">Gallery</a><a href=\"{route_prefix}/{gallery_id}/{prev_page}/\">Prev</a>"
            f"<span>{page_number} / {page_count}</span>{preview_label}<a href=\"{route_prefix}/{gallery_id}/{next_page}/\">Next</a></nav>"
            f'<a href="{route_prefix}/{gallery_id}/{next_page}/" aria-label="Next page">{image}</a>'
            "<script>document.addEventListener('keydown',function(e){"
            f"if(e.key==='ArrowLeft')location.href='{route_prefix}/{gallery_id}/{prev_page}/';"
            f"if(e.key==='ArrowRight')location.href='{route_prefix}/{gallery_id}/{next_page}/';"
            "});</script></body></html>"
        )

    def _preview_unavailable_html(self, gallery_id: str, error: str) -> str:
        return (
            "<!doctype html><html><head><meta charset=\"utf-8\"><title>Preview unavailable</title></head><body>"
            f"{self._local_menu_html()}<main><h1>Preview unavailable</h1><p>{html.escape(error)}</p>"
            f'<p><a href="/g/{gallery_id}/">Back to gallery</a></p></main></body></html>'
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
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass

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
                self._send_file(LOCAL_UI_CSS_PATH)
                return
            if path == f"{LOCAL_ASSET_PREFIX}/local.js":
                self._send_file(LOCAL_UI_JS_PATH)
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
            if path.startswith("/api/v2/"):
                try:
                    self._send_proxy(library.public_api_response(f"{path}{query}"))
                except PermissionError as exc:
                    self._send_text(str(exc), status=HTTPStatus.FORBIDDEN)
                except Exception as exc:  # noqa: BLE001 - surface read-only proxy failures.
                    self._send_text(f"upstream API fetch failed: {exc}", status=HTTPStatus.BAD_GATEWAY)
                return

            if path.rstrip("/") == "/downloads":
                try:
                    page = max(1, int(parse_qs(parsed.query).get("page", ["1"])[0]))
                except ValueError:
                    page = 1
                self._send_html(library.downloaded_galleries_html(page), extra_headers={"Cache-Control": "no-cache"})
                return
            if path.rstrip("/") == "/downloads/random":
                self._send_html(library.random_downloaded_html(), extra_headers={"Cache-Control": "no-store"})
                return
            if path.rstrip("/") == "/downloads/search":
                params = parse_qs(parsed.query)
                try:
                    page = max(1, int(params.get("page", ["1"])[0]))
                except ValueError:
                    page = 1
                self._send_html(
                    library.local_search_html(params.get("q", [""])[0], page),
                    extra_headers={"Cache-Control": "no-cache"},
                )
                return

            local_taxonomy_match = LOCAL_TAXONOMY_RE.fullmatch(path)
            if local_taxonomy_match:
                try:
                    page = max(1, int(parse_qs(parsed.query).get("page", ["1"])[0]))
                except ValueError:
                    page = 1
                rendered = library.local_taxonomy_html(
                    local_taxonomy_match.group(1), unquote(local_taxonomy_match.group(2)), page
                )
                if rendered is None:
                    self._send_text("not found", status=HTTPStatus.NOT_FOUND)
                else:
                    self._send_html(rendered, extra_headers={"Cache-Control": "no-cache"})
                return

            local_gallery_match = LOCAL_GALLERY_PAGE_RE.fullmatch(path)
            if local_gallery_match:
                rendered = library.local_gallery_html(local_gallery_match.group(1))
                if rendered is None:
                    self._send_text("not found", status=HTTPStatus.NOT_FOUND)
                else:
                    self._send_html(rendered, extra_headers={"Cache-Control": "no-cache"})
                return

            local_reader_match = LOCAL_GALLERY_READER_RE.fullmatch(path)
            if local_reader_match:
                rendered = library.local_reader_html(*local_reader_match.groups())
                if rendered is None:
                    self._send_text("not found", status=HTTPStatus.NOT_FOUND)
                else:
                    self._send_html(rendered, extra_headers={"Cache-Control": "no-cache"})
                return

            media_match = MEDIA_RE.fullmatch(path)
            if media_match:
                gallery_id, filename = media_match.groups()
                media_path = library.media_path(gallery_id, unquote(filename))
                if media_path is None:
                    self._send_text("not found", status=HTTPStatus.NOT_FOUND)
                    return
                self._send_file(media_path, cache_control="private, max-age=3600")
                return

            preview_match = PREVIEW_MEDIA_RE.fullmatch(path)
            if preview_match:
                gallery_id, page = preview_match.groups()
                try:
                    preview_path = library.preview_media_path(gallery_id, page)
                except Exception as exc:  # noqa: BLE001 - report preview fetch failures to the browser.
                    self._send_text(f"preview fetch failed: {exc}", status=HTTPStatus.BAD_GATEWAY)
                    return
                if preview_path is None:
                    self._send_text("not found", status=HTTPStatus.NOT_FOUND)
                    return
                self._send_file(preview_path, cache_control="private, max-age=3600")
                return

            thumbnail_match = PREVIEW_THUMB_RE.fullmatch(path)
            if thumbnail_match:
                gallery_id, page = thumbnail_match.groups()
                try:
                    thumbnail_path = library.preview_thumbnail_path(gallery_id, page)
                except Exception as exc:  # noqa: BLE001 - report upstream thumbnail failures.
                    self._send_text(f"preview thumbnail fetch failed: {exc}", status=HTTPStatus.BAD_GATEWAY)
                    return
                if thumbnail_path is None:
                    self._send_text("not found", status=HTTPStatus.NOT_FOUND)
                    return
                self._send_file(thumbnail_path, cache_control="private, max-age=86400")
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
                self._send_proxy(library.proxy_response(f"{path}{query}"))
            except LocalRedirect as redirect:
                self._send_redirect(redirect.location, no_store=redirect.no_store)
            except PermissionError as exc:
                self._send_text(str(exc), status=HTTPStatus.NOT_FOUND)
            except Exception as exc:  # noqa: BLE001 - surface proxy failures to the browser.
                self._send_text(f"upstream fetch failed: {exc}", status=HTTPStatus.BAD_GATEWAY)

        def do_POST(self) -> None:
            if not self._is_allowed():
                self._send_json({"error": "forbidden"}, status=HTTPStatus.FORBIDDEN)
                return
            path = urlparse(self.path).path
            if path not in {
                f"{LOCAL_API_PREFIX}/download",
                f"{LOCAL_API_PREFIX}/galleries/status",
                f"{LOCAL_API_PREFIX}/taxonomies/counts",
            }:
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
            if path.endswith("/taxonomies/counts"):
                taxonomies = payload.get("taxonomies")
                if (
                    not isinstance(taxonomies, list)
                    or len(taxonomies) > 100
                    or not all(
                        isinstance(item, dict)
                        and item.get("type") in GALLERY_TYPES
                        and isinstance(item.get("slug"), str)
                        and 0 < len(item["slug"]) <= 200
                        for item in taxonomies
                    )
                ):
                    self._send_json(
                        {"error": "taxonomies must contain at most 100 valid type/slug objects"},
                        status=HTTPStatus.BAD_REQUEST,
                    )
                    return
                values = [(str(item["type"]), str(item["slug"])) for item in taxonomies]
                self._send_json({"counts": library.database.taxonomy_counts(values)})
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

        def _send_html(
            self,
            payload: str,
            status: HTTPStatus = HTTPStatus.OK,
            *,
            extra_headers: dict[str, str] | None = None,
        ) -> None:
            self._send_bytes(payload.encode("utf-8"), "text/html; charset=utf-8", status=status, extra_headers=extra_headers)

        def _send_text(self, payload: str, status: HTTPStatus = HTTPStatus.OK) -> None:
            self._send_bytes(payload.encode("utf-8"), "text/plain; charset=utf-8", status=status)

        def _send_file(self, path: Path, *, cache_control: str | None = None) -> None:
            content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            with path.open("rb") as source:
                data = source.read()
            headers = {"Cache-Control": cache_control} if cache_control else None
            self._send_bytes(data, content_type, extra_headers=headers)

        def _send_proxy(self, response: ProxyResponse) -> None:
            extra_headers = {"X-NH-Cache": "stale"} if response.stale else None
            self._send_bytes(response.data, response.content_type, status=response.status, extra_headers=extra_headers)

        def _send_redirect(self, location: str, *, no_store: bool = False) -> None:
            self.send_response(HTTPStatus.FOUND)
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.send_header("Cache-Control", "no-store" if no_store else "private, max-age=900")
            self.end_headers()

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

        def _send_bytes(
            self,
            data: bytes,
            content_type: str,
            status: int = HTTPStatus.OK,
            *,
            extra_headers: dict[str, str] | None = None,
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            for name, value in (extra_headers or {}).items():
                self.send_header(name, value)
            if content_type.lower().startswith("text/html"):
                self.send_header(
                    "Content-Security-Policy",
                    "default-src 'self'; img-src 'self' data: blob: https://*.nhentai.net; "
                    "script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline' https://cdnjs.cloudflare.com; "
                    "font-src 'self' https://cdnjs.cloudflare.com; connect-src 'self'; frame-src 'none'; object-src 'none'",
                )
            self.end_headers()
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass

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
    parser.add_argument("--api-cache-ttl", type=int, help="public read-only API freshness lifetime in seconds")
    parser.add_argument("--html-cache-max-age", type=int, help="remove HTML cache entries unused for this many seconds")
    parser.add_argument("--html-cache-max-bytes", type=int, help="maximum combined HTML and metadata cache size")
    parser.add_argument("--extract-cache-max-bytes", type=int, help="maximum extracted image cache size")
    parser.add_argument("--preview-cache-max-age", type=int, help="remove temporary preview files unused for this many seconds")
    parser.add_argument("--preview-cache-max-bytes", type=int, help="maximum temporary preview cache size")
    parser.add_argument("--cache-sweep-interval", type=int, help="background cache cleanup interval in seconds")
    parser.add_argument(
        "--reindex-library",
        action="store_true",
        help="reconcile all CBZ metadata with SQLite, repair missing metadata, then exit",
    )
    parser.add_argument(
        "--refresh-gallery",
        metavar="ID",
        help="force an upstream metadata refresh for one downloaded gallery, then exit",
    )
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
        "NH_API_CACHE_TTL_SECONDS": args.api_cache_ttl,
        "NH_HTML_CACHE_MAX_AGE_SECONDS": args.html_cache_max_age,
        "NH_HTML_CACHE_MAX_BYTES": args.html_cache_max_bytes,
        "NH_EXTRACT_CACHE_MAX_BYTES": args.extract_cache_max_bytes,
        "NH_PREVIEW_CACHE_MAX_AGE_SECONDS": args.preview_cache_max_age,
        "NH_PREVIEW_CACHE_MAX_BYTES": args.preview_cache_max_bytes,
        "NH_CACHE_SWEEP_INTERVAL_SECONDS": args.cache_sweep_interval,
    }
    for name, value in cache_args.items():
        if value is not None:
            env[name] = str(value)

    maintenance_mode = args.reindex_library or args.refresh_gallery is not None
    manager = DownloadManager(
        project_root=PROJECT_ROOT,
        script_path=Path(args.download_script),
        storage_dir=expand_path(env.get("NH_FOLDER_PATH", str(Path.home() / "nh"))),
        env=env,
        autostart=not maintenance_mode,
    )
    networks = parse_networks(args.allowed_networks or DEFAULT_ALLOWED_NETWORKS)
    library = LocalLibrary(
        manager, env=env, cache_autostart=not maintenance_mode, index_autostart=not maintenance_mode
    )
    if maintenance_mode:
        if args.reindex_library:
            library.indexer.index_now(repair_remote=True)
            print(f"library index rebuilt at {library.database.path}")
        if args.refresh_gallery is not None:
            gallery_id = args.refresh_gallery
            if not is_valid_gallery_id(gallery_id):
                raise SystemExit("--refresh-gallery ID must contain digits only")
            archive = manager.archive_path(gallery_id)
            if not archive.exists():
                raise SystemExit(f"downloaded archive not found: {archive}")
            metadata = library._fetch_index_metadata(gallery_id)
            library.database.index_archive(archive, metadata=metadata, source_name="upstream")
            print(f"refreshed metadata for {gallery_id}")
        return
    handler = make_handler(manager, networks)
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
