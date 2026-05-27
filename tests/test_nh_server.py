import json
import os
import tempfile
import threading
import time
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path

from server.nh_server import (
    DownloadManager,
    DEFAULT_ALLOWED_NETWORKS,
    is_ip_allowed,
    is_valid_gallery_id,
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

    def request(self, method, path, body=None):
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        payload = None if body is None else json.dumps(body).encode("utf-8")
        headers = {"Content-Type": "application/json"} if payload else {}
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


if __name__ == "__main__":
    unittest.main()
