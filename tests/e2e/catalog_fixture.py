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


def page_png(width, height):
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack("!2I5B", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress((b"\0" + b"\x70\x80\x98" * width) * height)) + chunk(b"IEND", b""))


PNG = page_png(200, 280)
PORTRAIT = page_png(1200, 1800)
LANDSCAPE = page_png(1800, 900)


def metadata(gallery_id):
    return {
        "id": int(gallery_id),
        "title": {"english": "Fixture [Uncensored]" if str(gallery_id) == "9" else "Fixture title", "japanese": "無修正" if str(gallery_id) == "100" else ""},
        "tags": [{"id": 1, "type": "artist", "name": "Alice", "slug": "alice", "url": "/artist/alice/", "count": 100},
                 {"id": 2, "type": "language", "name": "chinese", "slug": "chinese"},
                 {"id": 3, "type": "language", "name": "english", "slug": "english"}],
        "pages": [{"number": n, "path": f"galleries/{gallery_id}/{n}.png", "thumbnail": f"galleries/{gallery_id}/{n}t.png"} for n in (1, 2)],
    }


class FixtureLibrary(LocalLibrary):
    def _fetch_nhentai(self, path):
        if path.startswith("/api/v2/galleries/"):
            data = json.dumps(metadata(path.split("/")[-1])).encode()
            return UpstreamResponse(data, "application/json", "utf-8", 200, path)
        if path.startswith("/search/") and "layout" in path:
            cards = ''.join(
                f'<div class="gallery lang-cn"><a class="cover" href="/g/{9 if i in (0, 5) else 2000+i}/" style="padding: 0 0 140% 0">'
                f'<img src="/fixture.png" width="200" height="280"><div class="caption">'
                + ('🇨🇳 ' if i == 2 else '')
                + ('A long complete title 中文標題 ' * (8 if i == 1 else 1) if i != 6 else 'LongUnbrokenTitle' * 15)
                + '</div></a></div>' for i in range(12)
            )
            content = f'<main><section class="index-container"><h2>All galleries</h2>{cards}</section></main>'
            # Reproduce absolute captions and inline aspect padding from the upstream gallery layout.
            source = ('<html><head><meta name="viewport" content="width=device-width,initial-scale=1">'
                      '<link rel="icon" href="/favicon.png"><style>'
                      'body{background:#111;color:#eee;margin:0}.index-container{max-width:1140px;margin:24px auto;padding:16px}'
                      '.gallery{display:inline-block;width:220px;height:345px;overflow:hidden}'
                      '.cover{position:relative;display:block}.cover img{position:absolute;top:0;width:100%}'
                      '.caption{position:absolute;bottom:0;height:32px;overflow:hidden;font:700 15px/15px Georgia,serif}.gallery:hover .caption{height:auto}'
                      '.gallery.lang-cn .caption:before{content:"";display:inline-block;float:left;width:24px;height:16px;background-image:url(/flags/cn.svg)}</style></head><body>' + content
                      + '<script>setTimeout(()=>{const grid=document.querySelector(".index-container");grid.innerHTML=grid.innerHTML},2400)</script>'
                      '<a class="logo"><img src="/logo.svg" alt="nhentai"></a></body></html>')
            return UpstreamResponse(source.encode(), "text/html", "utf-8", 200, path)
        if path.startswith("/g/"):
            content = '<main id="info"><h1>Remote Fixture</h1><div id="tags"><a href="/artist/alice/">Alice <span class="count" title="100 galleries">100</span></a></div></main>'
        else:
            content = '<main><div class="gallery"><a class="cover" href="/g/999/"><img width="200" height="280" src="/fixture.png" alt="Fixture [DECENSORED]"><div class="caption">Fixture [DECENSORED]</div></a></div></main>'
        source = f'<html><head></head><body>{content}</body></html>'
        return UpstreamResponse(source.encode(), "text/html", "utf-8", 200, path)

    def _fetch_cdn_image(self, remote_path, page_number):
        return PORTRAIT if page_number == 1 else LANDSCAPE

    def _detail_preview_metadata(self, gallery_id):
        return metadata(gallery_id)

with tempfile.TemporaryDirectory() as tmp:
    storage = Path(tmp)
    for gallery_id, stamp in ((9, 200), (100, 100)):
        archive = storage / f"{gallery_id}.cbz"
        with zipfile.ZipFile(archive, "w") as cbz:
            cbz.writestr("cover_page.html", f'<script>window._gallery = JSON.parse({json.dumps(json.dumps(metadata(gallery_id)))});</script>')
            for page in (1, 2):
                cbz.writestr(f"{page}.png", (PORTRAIT if page == 1 else LANDSCAPE) if gallery_id == 9 else PNG)
        os.utime(archive, (stamp, stamp))
    library = FixtureLibrary(DownloadManager(storage_dir=storage, autostart=False), cache_autostart=False)
    handler = make_library_handler(library, parse_networks(["127.0.0.1/32"]), base_path="/nh")
    handler.log_message = lambda *_args: None
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    signal.signal(signal.SIGTERM, lambda *_args: sys.exit(0))
    print(server.server_port, flush=True)
    server.serve_forever()
