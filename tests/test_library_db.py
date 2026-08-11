import html
import json
import os
import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path

from server.library_db import LibraryDatabase, LibraryIndexer, parse_gallery_metadata
from server.nh_server import DownloadManager, LocalLibrary


def legacy_html(metadata):
    return f'<script>window._gallery = JSON.parse({json.dumps(json.dumps(metadata))});</script>'


def write_gallery(storage: Path, gallery_id: int, metadata=None, *, stamp=None):
    archive = storage / f"{gallery_id}.cbz"
    source = legacy_html(metadata) if metadata is not None else '<meta itemprop="name" content="Fallback title">'
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("cover_page.html", source)
        zf.writestr("1.jpg", b"image")
    if stamp is not None:
        os.utime(archive, (stamp, stamp))
    return archive


class MetadataParserTests(unittest.TestCase):
    def test_parses_legacy_and_sveltekit_gallery_payloads(self):
        metadata = {"id": 123456, "title": {"pretty": "Example"}, "tags": []}
        self.assertEqual(parse_gallery_metadata(legacy_html(metadata), "123456"), metadata)

        envelope = html.escape(json.dumps({"body": json.dumps(metadata)}), quote=False)
        source = (
            '<script type="application/json" data-sveltekit-fetched '
            'data-url="/api/v2/galleries/123456?include=tags">'
            f"{envelope}</script>"
        )
        self.assertEqual(parse_gallery_metadata(source, "123456"), metadata)

    def test_rejects_wrong_gallery_id(self):
        self.assertIsNone(parse_gallery_metadata(legacy_html({"id": 1}), "2"))


class LibraryDatabaseTests(unittest.TestCase):
    def metadata(self, gallery_id, title, artist="alice"):
        types = ("tag", "artist", "character", "parody", "group", "language", "category")
        return {
            "id": gallery_id,
            "media_id": gallery_id + 10,
            "title": {"english": title, "japanese": "猫本", "pretty": f"Pretty {title}"},
            "cover": {"path": f"galleries/{gallery_id}/cover.jpg"},
            "pages": [
                {
                    "number": 1,
                    "path": f"galleries/{gallery_id}/1.jpg",
                    "thumbnail": f"galleries/{gallery_id}/1t.jpg",
                }
            ],
            "tags": [
                {
                    "id": position + 1,
                    "type": kind,
                    "name": artist if kind == "artist" else f"{kind}-name",
                    "slug": artist if kind == "artist" else f"{kind}-slug",
                    "url": f"/{kind}/{artist if kind == 'artist' else f'{kind}-slug'}/",
                }
                for position, kind in enumerate(types)
            ],
        }

    def test_indexes_all_taxonomy_types_and_partial_titles(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = Path(tmp)
            archive = write_gallery(storage, 123456, self.metadata(123456, "Alpha Beta"))
            database = LibraryDatabase(storage)

            self.assertEqual(database.index_archive(archive), "complete")
            record = database.gallery("123456")
            self.assertEqual({tag["type"] for tag in record["tags"]}, {
                "tag", "artist", "character", "parody", "group", "language", "category"
            })
            self.assertEqual(database.search("pha")[0][0]["id"], "123456")
            self.assertEqual(database.search("猫")[0][0]["id"], "123456")

    def test_search_and_taxonomy_are_id_descending_but_downloads_use_mtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = Path(tmp)
            database = LibraryDatabase(storage)
            newer_download = write_gallery(storage, 100001, self.metadata(100001, "Shared", "alice"), stamp=200)
            higher_id = write_gallery(storage, 200002, self.metadata(200002, "Shared", "alice"), stamp=100)
            database.index_archive(newer_download)
            database.index_archive(higher_id)

            self.assertEqual([item["id"] for item in database.downloaded()[0]], ["100001", "200002"])
            self.assertEqual([item["id"] for item in database.search("Shared")[0]], ["200002", "100001"])
            self.assertEqual([item["id"] for item in database.taxonomy("artist", "alice")[1]], ["200002", "100001"])
            self.assertEqual(database.gallery("200002")["tags"][1]["local_count"], 2)

    def test_fallback_is_pending_and_delete_cascades(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = Path(tmp)
            archive = write_gallery(storage, 123456)
            database = LibraryDatabase(storage)
            self.assertEqual(database.index_archive(archive), "pending")
            self.assertEqual(database.pending_ids(), ["123456"])
            database.delete_gallery("123456")
            self.assertIsNone(database.gallery("123456"))

    def test_same_taxonomy_slug_with_changed_upstream_id_is_merged_but_source_ids_are_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = Path(tmp)
            first_metadata = self.metadata(100001, "First")
            second_metadata = self.metadata(200002, "Second")
            second_metadata["tags"][1]["id"] = 999999
            first = write_gallery(storage, 100001, first_metadata)
            second = write_gallery(storage, 200002, second_metadata)
            database = LibraryDatabase(storage)

            database.index_archive(first)
            database.index_archive(second)

            name, records, total = database.taxonomy("artist", "alice")
            self.assertEqual(name, "alice")
            self.assertEqual(total, 2)
            self.assertEqual([item["id"] for item in records], ["200002", "100001"])
            self.assertEqual(
                [tag["id"] for tag in database.gallery("100001")["tags"] if tag["type"] == "artist"],
                [2],
            )
            self.assertEqual(
                [tag["id"] for tag in database.gallery("200002")["tags"] if tag["type"] == "artist"],
                [999999],
            )
            db = sqlite3.connect(database.path)
            try:
                aliases = db.execute(
                    "SELECT upstream_id,canonical_id FROM taxonomy_aliases WHERE taxonomy_type='artist' ORDER BY upstream_id"
                ).fetchall()
            finally:
                db.close()
            self.assertEqual(aliases, [(2, 2), (999999, 2)])

    def test_indexer_continues_after_one_archive_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = Path(tmp)
            first = write_gallery(storage, 200002, self.metadata(200002, "First"))
            second = write_gallery(storage, 100001, self.metadata(100001, "Second"))
            database = LibraryDatabase(storage)
            original = database.index_archive
            attempted = []

            def sometimes_fails(archive, **kwargs):
                attempted.append(archive.stem)
                if archive == first:
                    raise ValueError("fixture failure")
                return original(archive, **kwargs)

            database.index_archive = sometimes_fails
            indexer = LibraryIndexer(database, lambda _gallery_id: {}, autostart=False, request_interval=0)
            indexer.index_now(repair_remote=False)

            self.assertEqual(attempted, ["200002", "100001"])
            self.assertIsNone(database.gallery("200002"))
            self.assertIsNotNone(database.gallery("100001"))

    def test_local_pages_use_separate_routes_and_no_upstream_html(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = Path(tmp)
            write_gallery(storage, 123456, self.metadata(123456, "Local title"))
            manager = DownloadManager(storage_dir=storage, autostart=False)
            library = LocalLibrary(manager, cache_autostart=False)

            catalog = library.local_search_html("Local", 1)
            detail = library.local_gallery_html("123456")
            reader = library.local_reader_html("123456", "1")

            self.assertIn('href="/downloads/g/123456/"', catalog)
            self.assertIn('action="/downloads/search/"', catalog)
            self.assertIn('data-upstream-href="/artist/alice/"', detail)
            self.assertIn('data-local-href="/downloads/artist/alice/"', detail)
            self.assertIn('href="/downloads/artist/alice/"', detail)
            self.assertIn('<span class="nh-taxonomy-count">1</span>', detail)
            self.assertIn('src="/preview-thumbnail/123456/1"', detail)
            self.assertIn('/downloads/g/123456/1/', reader)

    def test_content_thumbnail_is_fetched_from_cdn_once_then_cached(self):
        class ThumbnailLibrary(LocalLibrary):
            def __init__(self, manager):
                super().__init__(manager, cache_autostart=False)
                self.thumbnail_fetches = []

            def _fetch_cdn_thumbnail(self, remote_path, page_number):
                self.thumbnail_fetches.append((remote_path, page_number))
                return b"thumbnail"

        with tempfile.TemporaryDirectory() as tmp:
            storage = Path(tmp)
            write_gallery(storage, 123456, self.metadata(123456, "Preview"))
            manager = DownloadManager(storage_dir=storage, autostart=False)
            library = ThumbnailLibrary(manager)

            first = library.preview_thumbnail_path("123456", "1")
            second = library.preview_thumbnail_path("123456", "1")

            self.assertEqual(first.read_bytes(), b"thumbnail")
            self.assertEqual(second, first)
            self.assertEqual(library.thumbnail_fetches, [("galleries/123456/1t.jpg", 1)])


class DownloadMetadataToleranceTests(unittest.TestCase):
    def test_successful_download_is_immediately_written_to_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            storage = root / "nh"
            fixture = root / "cover_page.html"
            fixture.write_text(
                legacy_html({
                    "id": 123456,
                    "media_id": 999,
                    "title": {"pretty": "Downloaded metadata"},
                    "tags": [{
                        "id": 12, "type": "artist", "name": "Alice", "slug": "alice",
                        "url": "/artist/alice/",
                    }],
                }),
                encoding="utf-8",
            )
            script = root / "downloader.sh"
            script.write_text(
                '#!/bin/sh\nmkdir -p "$NH_FOLDER_PATH/$1"\n'
                'cp "$NH_METADATA_FIXTURE" "$NH_FOLDER_PATH/$1/cover_page.html"\n'
                'printf image > "$NH_FOLDER_PATH/$1/1.jpg"\n',
                encoding="utf-8",
            )
            script.chmod(0o755)
            manager = DownloadManager(
                project_root=root,
                storage_dir=storage,
                env={
                    **os.environ,
                    "NH_FOLDER_PATH": str(storage),
                    "NH_METADATA_FIXTURE": str(fixture),
                },
                downloader_command=[str(script)],
            )
            library = LocalLibrary(manager, cache_autostart=False)

            job, _ = manager.submit("123456")
            manager.queue.join()

            self.assertEqual(manager.get_job(job.job_id).status, "succeeded")
            self.assertEqual(library.database.gallery("123456")["title"], "Downloaded metadata")
            self.assertEqual(library.database.gallery("123456")["metadata_status"], "complete")

    def test_metadata_failure_does_not_fail_completed_download(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            storage = root / "nh"
            script = root / "downloader.sh"
            script.write_text(
                '#!/bin/sh\nmkdir -p "$NH_FOLDER_PATH/$1"\nprintf image > "$NH_FOLDER_PATH/$1/1.jpg"\n',
                encoding="utf-8",
            )
            script.chmod(0o755)
            manager = DownloadManager(
                project_root=root,
                storage_dir=storage,
                env={**os.environ, "NH_FOLDER_PATH": str(storage)},
                downloader_command=[str(script)],
            )
            manager.archive_ready_callback = lambda _archive: (_ for _ in ()).throw(RuntimeError("metadata failed"))
            job, _ = manager.submit("123456")
            manager.queue.join()

            self.assertEqual(manager.get_job(job.job_id).status, "succeeded")
            self.assertTrue((storage / "123456.cbz").exists())
            self.assertTrue(any("Metadata pending" in line for line in manager.get_job(job.job_id).logs))


if __name__ == "__main__":
    unittest.main()
