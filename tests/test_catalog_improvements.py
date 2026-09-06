import os
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from server.library_db import LibraryDatabase, GALLERY_TYPES
from server.nh_server import DownloadManager, LocalLibrary, make_library_handler, parse_networks, prefix_html_paths
from server.search import close_spelling, load_aliases, query_terms, spelling_limit
from test_library_db import write_gallery


def metadata(gallery_id, title, tags=None):
    return {"id": gallery_id, "title": {"english": title, "japanese": "猫本", "pretty": title}, "tags": tags or []}


def taxonomy(kind, name, tag_id=1):
    return {"id": tag_id, "type": kind, "name": name, "slug": name.lower().replace(" ", "-")}


class SearchTests(unittest.TestCase):
    def test_all_metadata_fields_or_aliases_and_updates(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = Path(tmp)
            alias = storage / "aliases.yaml"
            alias.write_text('aliases:\n  - ["愛麗絲", "Alice Example"]\n  - ["Alice Example", "アリス"]\n', encoding="utf-8")
            with patch.dict(os.environ, {"NH_SEARCH_ALIASES_FILE": str(alias)}):
                db = LibraryDatabase(storage)
            for i, kind in enumerate(GALLERY_TYPES, 1):
                payload = metadata(i, "Unrelated title", [taxonomy(kind, f"Unique{kind}")])
                db.index_archive(write_gallery(storage, i, payload))
                self.assertEqual([r["id"] for r in db.search(f'"unique{kind}"')[0]], [str(i)])
            payload = metadata(100, "First Adventure", [taxonomy("character", "Alice Example", 100)])
            archive = write_gallery(storage, 100, payload, stamp=200)
            db.index_archive(archive)
            db.index_archive(write_gallery(storage, 101, metadata(101, "Other Story"), stamp=100))
            self.assertEqual([r["id"] for r in db.search("愛麗絲")[0]], ["100"])
            self.assertEqual([r["id"] for r in db.search("アリス")[0]], ["100"])
            self.assertEqual([r["id"] for r in db.search('"ＡＬＩＣＥ－ＥＸＡＭＰＬＥ"')[0]], ["100"])
            self.assertEqual([r["id"] for r in db.search("First Other")[0]], ["101", "100"])
            self.assertEqual(db.search("First Alice Adventure")[1], 1)
            self.assertEqual(db.search('"Adventure First"')[1], 0)
            self.assertEqual(db.search('"First Adventure"')[1], 1)
            self.assertEqual(db.search("Alcie")[1], 1)
            self.assertEqual(db.search('"Alcie"')[1], 0)
            self.assertEqual(db.search("猫")[1], 9)
            self.assertEqual(db.search("% _")[1], 0)
            payload = metadata(100, "Replacement", [taxonomy("character", "Bob", 100)])
            db.index_archive(archive, metadata=payload)
            self.assertEqual(db.search("愛麗絲")[1], 0)
            self.assertEqual(db.search("Replacement")[1], 1)
            db.delete_gallery("100")
            self.assertEqual(db.search("Replacement")[1], 0)
            with db._connect() as connection:
                self.assertEqual(connection.execute("SELECT count(*) FROM gallery_search_terms WHERE gallery_id=100").fetchone()[0], 0)

    def test_shared_taxonomy_rename_and_legacy_index_rebuild(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = Path(tmp)
            db = LibraryDatabase(storage)
            payload = metadata(1, "One", [taxonomy("artist", "Oldname")])
            first = write_gallery(storage, 1, payload)
            db.index_archive(first)
            db.index_archive(write_gallery(storage, 2, metadata(2, "Two", [taxonomy("artist", "Newname")])))
            self.assertEqual(db.search('"Newname"')[1], 2)
            self.assertEqual(db.search('"Oldname"')[1], 0)
            with db._connect() as connection:
                connection.execute("DROP TABLE gallery_search_terms")
                connection.execute("DROP TABLE gallery_search")
            db = LibraryDatabase(storage)
            self.assertEqual(db.search('"Newname"')[1], 2)

    def test_spelling_boundaries_and_alias_validation(self):
        self.assertEqual([spelling_limit(v) for v in ("cat", "cats", "example", "examples", "無修正作品")], [0, 1, 1, 2, 0])
        for word in ("alcie", "alic", "aalice", "alixe"):
            self.assertTrue(close_spelling("alice", word, 1))
        self.assertFalse(close_spelling("alice", "axixe", 1))
        self.assertTrue(close_spelling("examples", "exampels", 2))
        self.assertFalse(close_spelling("examples", "xxampxyz", 2))
        self.assertEqual(query_terms('"Alice Example"', [])[1], set())
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "aliases.yaml"
            self.assertEqual(load_aliases(path), [])
            path.write_text("aliases: []", encoding="utf-8")
            self.assertEqual(load_aliases(path), [])
            path.write_text("aliases: [broken]", encoding="utf-8")
            with self.assertRaises(ValueError):
                load_aliases(path)

    def test_sort_defaults_numeric_order_pagination_and_preserved_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = Path(tmp)
            db = LibraryDatabase(storage)
            for gallery_id, stamp in ((9, 300), (10, 100), (100, 100)):
                db.index_archive(write_gallery(storage, gallery_id, metadata(gallery_id, "Shared", [taxonomy("artist", "alice")]), stamp=stamp))
            ids = lambda rows: [r["id"] for r in rows]
            self.assertEqual(ids(db.downloaded()[0]), ["9", "100", "10"])
            self.assertEqual(ids(db.downloaded(sort="id")[0]), ["100", "10", "9"])
            self.assertEqual(ids(db.search("shared")[0]), ["100", "10", "9"])
            self.assertEqual(ids(db.search("shared", sort="downloaded")[0]), ["9", "100", "10"])
            self.assertEqual(ids(db.taxonomy("artist", "alice", sort="downloaded")[1]), ["9", "100", "10"])
            self.assertEqual(ids(db.search("shared", sort="downloaded", page=2, per_page=1)[0]), ["100"])
            db.set_downloaded_at("9", 400)
            db.index_archive(storage / "9.cbz")
            self.assertEqual(db.gallery("9")["downloaded_at"], 400)


class CatalogRouteTests(unittest.TestCase):
    def test_canonical_redirects_sort_empty_category_and_temp_reader(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = Path(tmp)
            write_gallery(storage, 9, metadata(9, "Shared"), stamp=300)
            write_gallery(storage, 100, metadata(100, "Shared"), stamp=100)
            library = LocalLibrary(DownloadManager(storage_dir=storage, autostart=False), cache_autostart=False)
            handler = make_library_handler(library, parse_networks(["127.0.0.1/32"]), base_path="/nh")
            httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()

            def get(path):
                conn = HTTPConnection("127.0.0.1", httpd.server_port, timeout=5)
                conn.request("GET", "/nh" + path)
                response = conn.getresponse()
                body = response.read().decode()
                conn.close()
                return response, body

            try:
                for old, target in (("/downloads/g/9?x=1", "/g/9/?x=1"), ("/downloads/g/9/1/", "/g/9/1/"), ("/downloads/g/999/", "/g/999/")):
                    response, _ = get(old)
                    self.assertEqual(response.status, 302)
                    self.assertEqual(response.getheader("Location"), "/nh" + target)
                for route in ("/downloads/", "/downloads/search/?q=Shared", "/downloads/artist/unknown/"):
                    response, body = get(route)
                    self.assertEqual(response.status, 200)
                    self.assertIn('class="nh-catalog-sort"', body)
                _, body = get("/downloads/search/?q=Shared&sort=downloaded&page=99")
                self.assertLess(body.index('href="/nh/g/9/"'), body.index('href="/nh/g/100/"'))
                self.assertIn('name="sort" value="downloaded"', body)
                self.assertIn('name="q" value="Shared"', body)
                _, body = get("/downloads/?sort=id")
                self.assertLess(body.index('href="/nh/g/100/"'), body.index('href="/nh/g/9/"'))
                _, body = get("/downloads/random/")
                self.assertNotIn('class="nh-catalog-sort"', body)
                with patch.object(library, "_detail_preview_metadata", return_value={}):
                    _, body = get("/g/9/")
                    self.assertIn('data-nh-downloaded-gallery="true"', body)
                with patch.object(library, "_preview_metadata", return_value={"pages": [{"number": 1}]}):
                    body = prefix_html_paths(library.reader_html("999", "1"), "/nh")
                    self.assertIn('id="nh-reader-next" href="/nh/g/999/"', body)
                    self.assertIn('aria-label="Next page"', body)
                    self.assertNotIn('rel="preload"', body)
            finally:
                httpd.shutdown()
                httpd.server_close()
                thread.join(timeout=2)
