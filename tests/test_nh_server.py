import json
import os
import tempfile
import threading
import time
import unittest
import zipfile
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path

from server.nh_server import (
    CacheSettings,
    DownloadManager,
    DEFAULT_ALLOWED_NETWORKS,
    LOCAL_UI_CSS,
    LocalLibrary,
    is_ip_allowed,
    is_valid_gallery_id,
    make_library_handler,
    make_handler,
    parse_networks,
)


class ValidationTests(unittest.TestCase):
    def test_gallery_id_must_be_digits(self):
        self.assertTrue(is_valid_gallery_id("123456"))
        self.assertFalse(is_valid_gallery_id("abc"))
        self.assertFalse(is_valid_gallery_id("123/456"))
        self.assertFalse(is_valid_gallery_id(123456))

    def test_allowed_networks(self):
        networks = parse_networks(DEFAULT_ALLOWED_NETWORKS)
        self.assertTrue(is_ip_allowed("192.168.50.144", networks))
        self.assertTrue(is_ip_allowed("192.168.193.144", networks))
        self.assertTrue(is_ip_allowed("127.0.0.1", networks))
        self.assertTrue(is_ip_allowed("100.109.167.26", networks))
        self.assertFalse(is_ip_allowed("100.1.2.3", networks))
        self.assertFalse(is_ip_allowed("192.168.51.144", networks))


class DownloadManagerTests(unittest.TestCase):
    def make_stub(self, directory: Path, body: str, exit_code: int = 0) -> Path:
        stub = directory / "stub_downloader.sh"
        stub.write_text(
            "#!/bin/bash\n"
            "set -e\n"
            "mkdir -p \"$NH_FOLDER_PATH/$1\"\n"
            f"{body}\n"
            f"exit {exit_code}\n",
            encoding="utf-8",
        )
        stub.chmod(0o755)
        return stub

    def wait_for_job(self, manager: DownloadManager, job_id: str):
        deadline = time.time() + 5
        while time.time() < deadline:
            job = manager.get_job(job_id)
            if job and job.status in {"succeeded", "failed"}:
                return job
            time.sleep(0.05)
        self.fail("job did not finish")

    def test_successful_download_is_packaged_as_cbz_and_folder_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            storage = root / "nh"
            stub = self.make_stub(root, 'echo "page" > "$NH_FOLDER_PATH/$1/1.txt"')
            manager = DownloadManager(
                project_root=root,
                storage_dir=storage,
                env={**os.environ, "NH_FOLDER_PATH": str(storage)},
                downloader_command=[str(stub)],
            )

            job, created = manager.submit("123456")
            self.assertTrue(created)
            job = self.wait_for_job(manager, job.job_id)

            self.assertEqual(job.status, "succeeded")
            self.assertTrue((storage / "123456.cbz").exists())
            self.assertFalse((storage / "123456").exists())

    def test_downloader_failure_keeps_job_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            storage = root / "nh"
            stub = self.make_stub(root, 'echo "fail"', exit_code=3)
            manager = DownloadManager(
                project_root=root,
                storage_dir=storage,
                env={**os.environ, "NH_FOLDER_PATH": str(storage)},
                downloader_command=[str(stub)],
            )

            job, _ = manager.submit("123456")
            job = self.wait_for_job(manager, job.job_id)

            self.assertEqual(job.status, "failed")
            self.assertEqual(job.return_code, 3)
            self.assertFalse((storage / "123456.cbz").exists())

    def test_existing_cbz_completes_immediately(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = Path(tmp) / "nh"
            storage.mkdir()
            (storage / "123456.cbz").write_bytes(b"ready")
            manager = DownloadManager(storage_dir=storage, autostart=False)

            job, created = manager.submit("123456")

            self.assertTrue(created)
            self.assertEqual(job.status, "succeeded")

    def test_gallery_status_reports_existing_cbz(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = Path(tmp) / "nh"
            storage.mkdir()
            (storage / "123456.cbz").write_bytes(b"ready")
            manager = DownloadManager(storage_dir=storage, autostart=False)

            status = manager.gallery_status("123456")

            self.assertEqual(status["id"], "123456")
            self.assertTrue(status["downloaded"])
            self.assertIsNone(status["status"])

    def test_duplicate_running_id_returns_existing_job(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            storage = root / "nh"
            stub = self.make_stub(root, 'sleep 0.5; echo "page" > "$NH_FOLDER_PATH/$1/1.txt"')
            manager = DownloadManager(
                project_root=root,
                storage_dir=storage,
                env={**os.environ, "NH_FOLDER_PATH": str(storage)},
                downloader_command=[str(stub)],
            )

            first, first_created = manager.submit("123456")
            second, second_created = manager.submit("123456")

            self.assertTrue(first_created)
            self.assertFalse(second_created)
            self.assertEqual(first.job_id, second.job_id)
            self.wait_for_job(manager, first.job_id)

    def test_different_ids_are_processed_one_at_a_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            storage = root / "nh"
            events = root / "events.log"
            stub = self.make_stub(
                root,
                'echo "start:$1" >> "$NH_EVENTS_LOG"; '
                'sleep 0.25; '
                'echo "page" > "$NH_FOLDER_PATH/$1/1.txt"; '
                'echo "end:$1" >> "$NH_EVENTS_LOG"',
            )
            manager = DownloadManager(
                project_root=root,
                storage_dir=storage,
                env={**os.environ, "NH_FOLDER_PATH": str(storage), "NH_EVENTS_LOG": str(events)},
                downloader_command=[str(stub)],
            )

            first, _ = manager.submit("111111")
            second, _ = manager.submit("222222")
            self.wait_for_job(manager, first.job_id)
            self.wait_for_job(manager, second.job_id)

            self.assertEqual(
                events.read_text(encoding="utf-8").splitlines(),
                ["start:111111", "end:111111", "start:222222", "end:222222"],
            )

    def test_queue_snapshot_tracks_running_queued_and_recent_jobs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            storage = root / "nh"
            stub = self.make_stub(root, 'sleep 0.25; echo "page" > "$NH_FOLDER_PATH/$1/1.txt"')
            manager = DownloadManager(
                project_root=root,
                storage_dir=storage,
                env={**os.environ, "NH_FOLDER_PATH": str(storage)},
                downloader_command=[str(stub)],
            )

            first, _ = manager.submit("111111")
            second, _ = manager.submit("222222")
            deadline = time.time() + 2
            snapshot = {}
            while time.time() < deadline:
                snapshot = manager.queue_snapshot()
                if snapshot["running"] and snapshot["queued"]:
                    break
                time.sleep(0.02)

            self.assertEqual(snapshot["running"][0]["id"], "111111")
            self.assertEqual(snapshot["queued"][0]["id"], "222222")
            self.wait_for_job(manager, first.job_id)
            self.wait_for_job(manager, second.job_id)
            recent_ids = [job["id"] for job in manager.queue_snapshot()["recent"]]
            self.assertIn("111111", recent_ids)
            self.assertIn("222222", recent_ids)


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.storage = self.root / "nh"
        self.stub = self.root / "stub_downloader.sh"
        self.stub.write_text(
            "#!/bin/bash\n"
            "set -e\n"
            "mkdir -p \"$NH_FOLDER_PATH/$1\"\n"
            "echo page > \"$NH_FOLDER_PATH/$1/1.txt\"\n",
            encoding="utf-8",
        )
        self.stub.chmod(0o755)
        self.manager = DownloadManager(
            project_root=self.root,
            storage_dir=self.storage,
            env={**os.environ, "NH_FOLDER_PATH": str(self.storage)},
            downloader_command=[str(self.stub)],
        )
        handler = make_handler(self.manager, parse_networks(["127.0.0.1/32"]))
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.httpd.server_address[1]

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)
        self.tmp.cleanup()

    def request(self, method, path, body=None, extra_headers=None):
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        payload = None if body is None else json.dumps(body).encode("utf-8")
        headers = {"Content-Type": "application/json"} if payload else {}
        headers.update(extra_headers or {})
        conn.request(method, path, body=payload, headers=headers)
        response = conn.getresponse()
        data = json.loads(response.read().decode("utf-8"))
        conn.close()
        return response.status, data

    def test_download_request_and_job_status(self):
        status, data = self.request("POST", "/api/download", {"id": "123456"})
        self.assertEqual(status, 202)
        self.assertEqual(data["id"], "123456")
        self.assertIn(data["status"], {"queued", "running", "succeeded"})

        deadline = time.time() + 5
        while time.time() < deadline:
            status, job = self.request("GET", f"/api/jobs/{data['job_id']}")
            self.assertEqual(status, 200)
            if job["status"] == "succeeded":
                break
            time.sleep(0.05)
        self.assertEqual(job["status"], "succeeded")

    def test_invalid_id_is_rejected(self):
        status, data = self.request("POST", "/api/download", {"id": "../123"})
        self.assertEqual(status, 400)
        self.assertIn("error", data)

    def test_gallery_status_request(self):
        self.storage.mkdir(parents=True)
        (self.storage / "123456.cbz").write_bytes(b"ready")

        status, data = self.request("POST", "/api/galleries/status", {"ids": ["123456", "654321"]})

        self.assertEqual(status, 200)
        self.assertTrue(data["galleries"]["123456"]["downloaded"])
        self.assertFalse(data["galleries"]["654321"]["downloaded"])

    def test_gallery_status_rejects_invalid_ids(self):
        status, data = self.request("POST", "/api/galleries/status", {"ids": ["123456", "../bad"]})
        self.assertEqual(status, 400)
        self.assertIn("error", data)

    def test_queue_endpoint_returns_snapshot(self):
        self.manager.submit("123456")

        status, data = self.request("GET", "/api/queue")

        self.assertEqual(status, 200)
        self.assertIn("running", data)
        self.assertIn("queued", data)
        self.assertIn("recent", data)
        self.assertIn("counts", data)

    def test_delete_gallery_removes_archive_and_local_cache(self):
        self.storage.mkdir(parents=True)
        (self.storage / "123456.cbz").write_bytes(b"ready")
        cache_file = self.storage / ".nh-local" / "html" / "123456" / "cover_page.html"
        cache_file.parent.mkdir(parents=True)
        cache_file.write_text("cached", encoding="utf-8")
        metadata_file = self.storage / ".nh-local" / "metadata" / "123456.json"
        metadata_file.parent.mkdir(parents=True)
        metadata_file.write_text("{}", encoding="utf-8")
        extract_file = self.storage / ".nh-local" / "extract" / "123456" / "1.jpg"
        extract_file.parent.mkdir(parents=True)
        extract_file.write_bytes(b"image")

        status, data = self.request("DELETE", "/api/galleries/123456")

        self.assertEqual(status, 200)
        self.assertTrue(data["deleted"])
        self.assertFalse((self.storage / "123456.cbz").exists())
        self.assertFalse(cache_file.exists())
        self.assertFalse(metadata_file.exists())
        self.assertFalse(extract_file.exists())

    def test_delete_gallery_rejects_invalid_id(self):
        status, data = self.request("DELETE", "/api/galleries/../bad")
        self.assertEqual(status, 400)
        self.assertIn("error", data)

    def test_browser_origin_is_rejected_but_extension_origin_is_allowed(self):
        status, _ = self.request("GET", "/health", extra_headers={"Origin": "https://evil.example"})
        self.assertEqual(status, 403)

        status, data = self.request("GET", "/health", extra_headers={"Origin": "moz-extension://test"})
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])


class StubLibrary(LocalLibrary):
    def __init__(self, manager, responses, cache_settings=None):
        super().__init__(manager, cache_settings=cache_settings, cache_autostart=False)
        self.responses = responses
        self.fetches = []

    def _fetch_nhentai_html(self, path_with_query: str) -> str:
        self.fetches.append(path_with_query)
        if path_with_query not in self.responses:
            raise RuntimeError(f"unexpected fetch: {path_with_query}")
        return self.responses[path_with_query]


class LocalLibraryTests(unittest.TestCase):
    def small_settings(self, **overrides):
        values = {
            "html_ttl_seconds": 60,
            "html_max_age_seconds": 100,
            "html_max_bytes": 10_000,
            "extract_max_bytes": 10_000,
            "sweep_interval_seconds": 100,
        }
        values.update(overrides)
        return CacheSettings(**values)

    def test_rewrite_links_preserves_cdn_images(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = DownloadManager(storage_dir=Path(tmp), autostart=False)
            library = LocalLibrary(manager)

            html = (
                "<html><head></head><body>"
                '<a href="/g/123456/">gallery</a>'
                '<a href="https://nhentai.net/artist/example/">artist</a>'
                '<img src="https://t1.nhentai.net/galleries/999/cover.jpg">'
                "</body></html>"
            )

            rewritten = library.rewrite_html(html)

            self.assertIn('href="/g/123456/"', rewritten)
            self.assertIn('href="/artist/example/"', rewritten)
            self.assertIn('src="https://t1.nhentai.net/galleries/999/cover.jpg"', rewritten)
            self.assertIn("data-nh-local-navigation", rewritten)
            self.assertIn("/_nh-local/assets/local.js", rewritten)

    def test_rewrite_removes_tracking_script_and_iframe(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = DownloadManager(storage_dir=Path(tmp), autostart=False)
            library = LocalLibrary(manager, cache_autostart=False)
            source = (
                '<html><head><meta http-equiv="delegate-ch" content="https://tsyndicate.com">'
                '<script src="https://static.cloudflareinsights.com/beacon.js"></script></head>'
                '<body><iframe src="https://ads.example"></iframe>'
                '<a href="//tsyndicate.com/ad">Ad</a>'
                '<li class="menu-sign-in"><a href="/login/">Login</a></li></body></html>'
            )

            rendered = library.rewrite_html(source)

            self.assertNotIn("cloudflareinsights", rendered)
            self.assertNotIn("tsyndicate", rendered)
            self.assertNotIn("<iframe", rendered)
            self.assertNotIn('href="/login/"', rendered)
            self.assertIn('a[href^="/login"]', LOCAL_UI_CSS)

    def test_fresh_html_cache_avoids_second_upstream_fetch(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = DownloadManager(storage_dir=Path(tmp), autostart=False)
            library = StubLibrary(manager, {"/": "<html><head></head><body>home</body></html>"})

            library.proxy_html("/")
            library.proxy_html("/")

            self.assertEqual(library.fetches, ["/"])

    def test_expired_html_uses_stale_copy_when_refresh_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = DownloadManager(storage_dir=Path(tmp), autostart=False)
            settings = CacheSettings(html_ttl_seconds=1, html_max_age_seconds=100, html_max_bytes=10000, extract_max_bytes=10000, sweep_interval_seconds=100)
            library = StubLibrary(manager, {"/": "<html><head></head><body>home</body></html>"}, settings)
            library.proxy_html("/")
            cache_path = library.cache.proxy_path("/")
            old = time.time() - 5
            os.utime(cache_path, (old, old))
            library.responses.clear()

            rendered = library.proxy_html("/")

            self.assertIn("showing cached content", rendered)

    def test_non_public_account_route_is_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = DownloadManager(storage_dir=Path(tmp), autostart=False)
            library = StubLibrary(manager, {})

            with self.assertRaises(PermissionError):
                library.proxy_html("/login/")

    def test_html_cache_enforces_size_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = DownloadManager(storage_dir=Path(tmp), autostart=False)
            library = StubLibrary(manager, {}, self.small_settings(html_max_bytes=20))
            first = manager.local_cache_root() / "proxy" / "first.html"
            second = manager.local_cache_root() / "proxy" / "second.html"

            library.cache.write_html(first, "a" * 15)
            time.sleep(0.01)
            library.cache.write_html(second, "b" * 15)

            files = list((manager.local_cache_root() / "proxy").glob("*.html"))
            self.assertLessEqual(sum(path.stat().st_size for path in files), 20)
            self.assertTrue(second.exists())

    def test_extract_cache_evicts_oldest_complete_gallery(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = DownloadManager(storage_dir=Path(tmp), autostart=False)
            library = StubLibrary(manager, {}, self.small_settings(extract_max_bytes=12))
            old_dir = manager.local_extract_dir("111111")
            new_dir = manager.local_extract_dir("222222")
            for directory in (old_dir, new_dir):
                directory.mkdir(parents=True)
                (directory / "1.jpg").write_bytes(b"1234567890")
                (directory / ".complete").write_text("ok")
            old = time.time() - 10
            os.utime(old_dir / ".complete", (old, old))

            library.cache.cleanup()

            self.assertFalse(old_dir.exists())
            self.assertTrue(new_dir.exists())

    def test_gallery_html_fetches_and_caches_metadata_on_demand(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = DownloadManager(storage_dir=Path(tmp), autostart=False)
            metadata = {"id": 123456, "title": {"pretty": "Cached Title"}}
            source = f"<script>window._gallery = JSON.parse({json.dumps(json.dumps(metadata))});</script>"
            library = StubLibrary(manager, {"/g/123456/": source})

            rendered = library.gallery_html("123456")

            self.assertIn("window._gallery", rendered)
            cached = json.loads(manager.local_metadata_path("123456").read_text(encoding="utf-8"))
            self.assertEqual(cached["title"]["pretty"], "Cached Title")
            self.assertTrue((manager.local_html_dir("123456") / "cover_page.html").exists())

    def test_reader_uses_local_cbz_image_when_available(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = Path(tmp)
            manager = DownloadManager(storage_dir=storage, autostart=False)
            with zipfile.ZipFile(storage / "123456.cbz", "w") as zf:
                zf.writestr("1.jpg", b"image")
            library = StubLibrary(manager, {})

            rendered = library.reader_html("123456", "1")
            media_path = library.media_path("123456", "1.jpg")

            self.assertIn("/media/123456/1.jpg", rendered)
            self.assertIsNotNone(media_path)
            self.assertEqual(media_path.read_bytes(), b"image")

    def test_reader_without_cbz_never_fetches_remote_reader(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = DownloadManager(storage_dir=Path(tmp), autostart=False)
            library = StubLibrary(manager, {"/g/123456/1/": "remote"})

            rendered = library.reader_html("123456", "1")

            self.assertIn("not downloaded", rendered)
            self.assertEqual(library.fetches, [])

    def test_nested_cbz_is_extracted_without_removing_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = Path(tmp)
            archive = storage / "123456.cbz"
            with zipfile.ZipFile(archive, "w") as zf:
                zf.writestr("123456/1.jpg", b"one")
                zf.writestr("../escape.jpg", b"escape")
                zf.writestr("123456/readme.txt", b"ignored")
            manager = DownloadManager(storage_dir=storage, autostart=False)
            library = StubLibrary(manager, {})

            image = library.page_image_path("123456", "1")

            self.assertEqual(image.read_bytes(), b"one")
            self.assertTrue(archive.exists())
            self.assertFalse((storage / ".nh-local" / "extract" / "escape.jpg").exists())

    def test_library_media_route_serves_extracted_cbz_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = Path(tmp)
            manager = DownloadManager(storage_dir=storage, autostart=False)
            with zipfile.ZipFile(storage / "123456.cbz", "w") as zf:
                zf.writestr("1.jpg", b"image")
            library = StubLibrary(manager, {})
            handler = make_library_handler(library, parse_networks(["127.0.0.1/32"]))
            httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()

            try:
                conn = HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=5)
                conn.request("GET", "/media/123456/1.jpg")
                response = conn.getresponse()
                body = response.read()
                conn.close()
            finally:
                httpd.shutdown()
                httpd.server_close()
                thread.join(timeout=2)

            self.assertEqual(response.status, 200)
            self.assertEqual(body, b"image")

    def test_library_same_origin_status_api_and_local_asset(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = Path(tmp)
            (storage / "123456.cbz").write_bytes(b"ready")
            manager = DownloadManager(storage_dir=storage, autostart=False)
            library = StubLibrary(manager, {})
            handler = make_library_handler(library, parse_networks(["127.0.0.1/32"]))
            httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            try:
                conn = HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=5)
                payload = json.dumps({"ids": ["123456", "654321"]}).encode()
                conn.request(
                    "POST",
                    "/_nh-local/api/galleries/status",
                    body=payload,
                    headers={"Content-Type": "application/json"},
                )
                response = conn.getresponse()
                body = json.loads(response.read())
                conn.close()
                conn = HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=5)
                conn.request("GET", "/_nh-local/assets/local.js")
                asset_response = conn.getresponse()
                asset = asset_response.read()
                conn.close()
            finally:
                httpd.shutdown()
                httpd.server_close()
                thread.join(timeout=2)

            self.assertEqual(response.status, 200)
            self.assertTrue(body["galleries"]["123456"]["downloaded"])
            self.assertEqual(asset_response.status, 200)
            self.assertIn(b"/_nh-local/api", asset)


if __name__ == "__main__":
    unittest.main()
