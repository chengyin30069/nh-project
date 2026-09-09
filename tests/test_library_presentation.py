import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.request import urlopen

from server.library_db import GALLERY_TYPES, LibraryDatabase
from server.nh_server import DownloadManager, LocalLibrary, TAXONOMY_DIRECTORIES, make_library_handler, parse_networks
from test_library_db import write_gallery


def tag(kind, name, number=1):
    return {"id": number, "type": kind, "name": name, "slug": name.lower().replace(" ", "-")}


def metadata(number, tags):
    return {"id": number, "title": {"english": "A <safe> title"}, "tags": tags}


class LibraryPresentationTests(unittest.TestCase):
    def test_directory_counts_aliases_search_paging_and_deletion(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = Path(tmp)
            db = LibraryDatabase(storage)
            for number in (1, 2):
                # Different upstream IDs for the same slug must count books, not IDs.
                db.index_archive(write_gallery(storage, number, metadata(number, [tag(kind, "Shared", number) for kind in GALLERY_TYPES])))
            db.index_archive(write_gallery(storage, 3, metadata(3, [tag("tag", "Ａlpha Example", 3), tag("tag", "100%_literal", 4)])))
            for kind in GALLERY_TYPES:
                rows, total = db.taxonomy_directory(kind)
                self.assertEqual(rows[0], {"name": "Shared", "slug": "shared", "count": 2})
                self.assertEqual(total, 3 if kind == "tag" else 1)
            self.assertEqual(db.taxonomy_directory("tag", query="ALPHA-example")[1], 1)
            self.assertEqual(db.taxonomy_directory("tag", query="%_")[1], 1)
            self.assertEqual(db.taxonomy_directory("tag", query="missing"), ([], 0))
            self.assertEqual(db.taxonomy_directory("invalid"), ([], 0))
            rows, total = db.taxonomy_directory("tag", sort="name", per_page=1, page=999)
            self.assertEqual(total, 3)
            self.assertEqual(rows[0]["slug"], "shared")
            db.delete_gallery("1")
            self.assertEqual(db.taxonomy_directory("artist")[0][0]["count"], 1)
            db.delete_gallery("2")
            self.assertEqual(db.taxonomy_directory("artist"), ([], 0))

    def test_language_prefixes_in_every_local_catalog_and_missing_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = Path(tmp)
            tags = [tag("language", name, i) for i, name in enumerate(("chinese", "english", "japanese", "translated", "unknown"), 1)]
            write_gallery(storage, 1, metadata(1, tags))
            write_gallery(storage, 2, metadata(2, []))
            library = LocalLibrary(DownloadManager(storage_dir=storage, autostart=False), cache_autostart=False)
            pages = [library.downloaded_galleries_html(1), library.random_downloaded_html(),
                     library.local_search_html("safe", 1), library.local_taxonomy_html("language", "chinese", 1)]
            for source in pages:
                self.assertEqual(source.count('class="nh-language-flags"'), 1)
                self.assertIn("🇹🇼 🇬🇧 🇯🇵", source)
                self.assertNotIn("🇨🇳", source)
                self.assertIn("A &lt;safe&gt; title", source)
            self.assertEqual(library.database.languages([]), {})
            self.assertNotIn("2", library.database.languages(["1", "2"]))

    def test_directory_http_routes_prefix_empty_and_query_escaping(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = Path(tmp)
            write_gallery(storage, 1, metadata(1, [tag(kind, "Shared") for kind in GALLERY_TYPES]))
            library = LocalLibrary(DownloadManager(storage_dir=storage, autostart=False), cache_autostart=False)
            handler = make_library_handler(library, parse_networks(["127.0.0.1/32"]), base_path="/nh")
            handler.log_message = lambda *_args: None
            with ThreadingHTTPServer(("127.0.0.1", 0), handler) as server:
                worker = threading.Thread(target=server.serve_forever)
                worker.start()
                try:
                    for directory, kind in TAXONOMY_DIRECTORIES.items():
                        with urlopen(f"http://127.0.0.1:{server.server_port}/nh/downloads/{directory}/?page=invalid&sort=invalid") as response:
                            source = response.read().decode()
                            self.assertEqual(response.headers["Cache-Control"], "no-cache")
                        self.assertIn(f'href="/nh/downloads/{kind}/shared/"', source)
                        self.assertIn('<option value="count" selected>', source)
                        self.assertIn('Page 1 / 1', source)
                    source = library.taxonomy_directory_html("tags", '\"><script>bad</script>', 999, "name")
                    self.assertNotIn('<script>bad', source)
                    self.assertIn('No downloaded classifications found.', source)
                    self.assertIn('Page 1 / 1', source)
                finally:
                    server.shutdown()
                    worker.join()


if __name__ == "__main__":
    unittest.main()
