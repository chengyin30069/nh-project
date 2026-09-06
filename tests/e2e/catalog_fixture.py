"""Deterministic actual-server fixture; every upstream response is synthetic."""

import json
import os
import signal
import struct
import sys
import tempfile
import zipfile
import zlib
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from server.nh_server import DownloadManager, LocalLibrary, UpstreamResponse, make_library_handler, parse_networks

def chunk(kind, data):
    return struct.pack("!I", len(data)) + kind + data + struct.pack("!I", zlib.crc32(kind + data))


# A normal-sized synthetic page makes mouse hit testing representative of a reader.
PNG = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack("!2I5B", 200, 280, 8, 2, 0, 0, 0))
       + chunk(b"IDAT", zlib.compress((b"\0" + b"\x80\x80\x80" * 200) * 280)) + chunk(b"IEND", b""))


def metadata(gallery_id):
    return {
        "id": int(gallery_id),
        "title": {"english": "Fixture [Uncensored]" if str(gallery_id) == "9" else "Fixture title", "japanese": "無修正" if str(gallery_id) == "100" else ""},
        "tags": [{"id": 1, "type": "artist", "name": "Alice", "slug": "alice", "url": "/artist/alice/", "count": 100}],
        "pages": [{"number": n, "path": f"galleries/{gallery_id}/{n}.png", "thumbnail": f"galleries/{gallery_id}/{n}t.png"} for n in (1, 2)],
    }


class FixtureLibrary(LocalLibrary):
    def _fetch_nhentai(self, path):
        if path.startswith("/api/v2/galleries/"):
            data = json.dumps(metadata(path.split("/")[-1])).encode()
            return UpstreamResponse(data, "application/json", "utf-8", 200, path)
        if path.startswith("/g/"):
            content = '<main id="info"><h1>Remote Fixture</h1><div id="tags"><a href="/artist/alice/">Alice <span class="count" title="100 galleries">100</span></a></div></main>'
        else:
            content = '<main><div class="gallery"><a class="cover" href="/g/999/"><img width="200" height="280" src="/fixture.png" alt="Fixture [DECENSORED]"><div class="caption">Fixture [DECENSORED]</div></a></div></main>'
        source = f'<html><head></head><body>{content}</body></html>'
        return UpstreamResponse(source.encode(), "text/html", "utf-8", 200, path)

    def _fetch_cdn_image(self, remote_path, page_number):
        return PNG

    def _detail_preview_metadata(self, gallery_id):
        return metadata(gallery_id)

with tempfile.TemporaryDirectory() as tmp:
    storage = Path(tmp)
    for gallery_id, stamp in ((9, 200), (100, 100)):
        archive = storage / f"{gallery_id}.cbz"
        with zipfile.ZipFile(archive, "w") as cbz:
            cbz.writestr("cover_page.html", f'<script>window._gallery = JSON.parse({json.dumps(json.dumps(metadata(gallery_id)))});</script>')
            for page in (1, 2):
                cbz.writestr(f"{page}.png", PNG)
        os.utime(archive, (stamp, stamp))
    library = FixtureLibrary(DownloadManager(storage_dir=storage, autostart=False), cache_autostart=False)
    handler = make_library_handler(library, parse_networks(["127.0.0.1/32"]), base_path="/nh")
    handler.log_message = lambda *_args: None
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    signal.signal(signal.SIGTERM, lambda *_args: sys.exit(0))
    print(server.server_port, flush=True)
    server.serve_forever()
