"""Persistent metadata index for downloaded nhentai galleries."""

from __future__ import annotations

import html
import json
import os
import re
import sqlite3
import threading
import time
import zipfile
from pathlib import Path
from typing import Callable


GALLERY_TYPES = ("tag", "artist", "character", "parody", "group", "language", "category")
WINDOW_GALLERY_RE = re.compile(r"window\._gallery\s*=\s*JSON\.parse\((?P<value>\"(?:\\.|[^\"\\])*\")\)")
SVELTE_FETCHED_RE = re.compile(
    r"<script(?P<attrs>[^>]*)data-sveltekit-fetched(?P<attrs2>[^>]*)>(?P<body>.*?)</script>",
    re.IGNORECASE | re.DOTALL,
)
ARCHIVE_COVER_RE = re.compile(
    r'<meta\s+itemprop=["\']image["\']\s+content=["\'](?P<url>(?://|https://)[^"\']+)["\']',
    re.IGNORECASE,
)
ARCHIVE_TITLE_RE = re.compile(
    r'<meta\s+itemprop=["\']name["\']\s+content=["\'](?P<title>[^"\']+)["\']',
    re.IGNORECASE,
)


class ClosingConnection(sqlite3.Connection):
    """sqlite3 context manager that also releases its file descriptor."""

    def __exit__(self, exc_type, exc_value, traceback):  # type: ignore[no-untyped-def]
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def parse_gallery_metadata(source: str, expected_id: str | None = None) -> dict[str, object] | None:
    """Extract a gallery API object from legacy or SvelteKit cover HTML."""

    match = WINDOW_GALLERY_RE.search(source)
    if match:
        try:
            value = json.loads(json.loads(match.group("value")))
            if isinstance(value, dict) and (expected_id is None or str(value.get("id")) == expected_id):
                return value
        except (json.JSONDecodeError, TypeError):
            pass

    for match in SVELTE_FETCHED_RE.finditer(source):
        attrs = f"{match.group('attrs')} {match.group('attrs2')}"
        if "/api/v2/galleries/" not in html.unescape(attrs):
            continue
        try:
            envelope = json.loads(html.unescape(match.group("body")))
            body = envelope.get("body") if isinstance(envelope, dict) else None
            value = json.loads(body) if isinstance(body, str) else body
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(value, dict) and (expected_id is None or str(value.get("id")) == expected_id):
            return value
    return None


def fallback_gallery_metadata(source: str, gallery_id: str) -> dict[str, object]:
    """Build a deliberately incomplete record when an archive has no API object."""

    title_match = ARCHIVE_TITLE_RE.search(source)
    cover_match = ARCHIVE_COVER_RE.search(source)
    cover_url = cover_match.group("url") if cover_match else ""
    if cover_url.startswith("//"):
        cover_url = f"https:{cover_url}"
    return {
        "id": int(gallery_id),
        "title": {"pretty": html.unescape(title_match.group("title")) if title_match else f"Gallery {gallery_id}"},
        "cover_url": cover_url,
        "tags": [],
    }


class LibraryDatabase:
    """SQLite-backed catalog containing only galleries with local CBZ files."""

    def __init__(self, storage_dir: Path) -> None:
        self.storage_dir = storage_dir
        self.path = storage_dir / ".nh-local" / "library.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._write_lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, factory=ClosingConnection)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS galleries (
                    id INTEGER PRIMARY KEY,
                    media_id INTEGER,
                    title_english TEXT NOT NULL DEFAULT '',
                    title_japanese TEXT NOT NULL DEFAULT '',
                    title_pretty TEXT NOT NULL DEFAULT '',
                    cover_url TEXT NOT NULL DEFAULT '',
                    cover_path TEXT NOT NULL DEFAULT '',
                    downloaded_at REAL NOT NULL,
                    archive_mtime_ns INTEGER NOT NULL,
                    archive_size INTEGER NOT NULL,
                    metadata_status TEXT NOT NULL CHECK(metadata_status IN ('complete','pending')),
                    metadata_source TEXT NOT NULL,
                    indexed_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS taxonomies (
                    id INTEGER NOT NULL,
                    type TEXT NOT NULL,
                    name TEXT NOT NULL,
                    slug TEXT NOT NULL,
                    upstream_url TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (id, type),
                    UNIQUE (type, slug)
                );
                CREATE TABLE IF NOT EXISTS gallery_taxonomies (
                    gallery_id INTEGER NOT NULL REFERENCES galleries(id) ON DELETE CASCADE,
                    taxonomy_id INTEGER NOT NULL,
                    taxonomy_type TEXT NOT NULL,
                    source_taxonomy_id INTEGER,
                    position INTEGER NOT NULL,
                    PRIMARY KEY (gallery_id, taxonomy_id, taxonomy_type),
                    FOREIGN KEY (taxonomy_id, taxonomy_type) REFERENCES taxonomies(id, type) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS taxonomy_aliases (
                    taxonomy_type TEXT NOT NULL,
                    upstream_id INTEGER NOT NULL,
                    canonical_id INTEGER NOT NULL,
                    PRIMARY KEY (taxonomy_type, upstream_id),
                    FOREIGN KEY (canonical_id, taxonomy_type) REFERENCES taxonomies(id, type) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS gallery_download_order ON galleries(downloaded_at DESC, id DESC);
                CREATE INDEX IF NOT EXISTS gallery_taxonomy_lookup ON gallery_taxonomies(taxonomy_type, taxonomy_id, gallery_id DESC);
                CREATE VIRTUAL TABLE IF NOT EXISTS gallery_titles_fts USING fts5(
                    title_english, title_japanese, title_pretty,
                    content='galleries', content_rowid='id', tokenize='trigram'
                );
                CREATE TRIGGER IF NOT EXISTS gallery_titles_insert AFTER INSERT ON galleries BEGIN
                    INSERT INTO gallery_titles_fts(rowid,title_english,title_japanese,title_pretty)
                    VALUES(new.id,new.title_english,new.title_japanese,new.title_pretty);
                END;
                CREATE TRIGGER IF NOT EXISTS gallery_titles_delete AFTER DELETE ON galleries BEGIN
                    INSERT INTO gallery_titles_fts(gallery_titles_fts,rowid,title_english,title_japanese,title_pretty)
                    VALUES('delete',old.id,old.title_english,old.title_japanese,old.title_pretty);
                END;
                CREATE TRIGGER IF NOT EXISTS gallery_titles_update AFTER UPDATE OF title_english,title_japanese,title_pretty ON galleries BEGIN
                    INSERT INTO gallery_titles_fts(gallery_titles_fts,rowid,title_english,title_japanese,title_pretty)
                    VALUES('delete',old.id,old.title_english,old.title_japanese,old.title_pretty);
                    INSERT INTO gallery_titles_fts(rowid,title_english,title_japanese,title_pretty)
                    VALUES(new.id,new.title_english,new.title_japanese,new.title_pretty);
                END;
                """
            )
            columns = {row[1] for row in db.execute("PRAGMA table_info(gallery_taxonomies)")}
            if "source_taxonomy_id" not in columns:
                db.execute("ALTER TABLE gallery_taxonomies ADD COLUMN source_taxonomy_id INTEGER")
            db.execute("UPDATE gallery_taxonomies SET source_taxonomy_id=taxonomy_id WHERE source_taxonomy_id IS NULL")
            db.execute(
                """INSERT OR IGNORE INTO taxonomy_aliases(taxonomy_type,upstream_id,canonical_id)
                SELECT type,id,id FROM taxonomies"""
            )

    @staticmethod
    def archive_source(archive: Path) -> str:
        try:
            with zipfile.ZipFile(archive) as zf:
                candidates = [name for name in zf.namelist() if Path(name).name == "cover_page.html"]
                return zf.read(sorted(candidates)[0]).decode("utf-8", "replace") if candidates else ""
        except (OSError, zipfile.BadZipFile, KeyError):
            return ""

    def archive_needs_index(self, archive: Path) -> bool:
        try:
            stat = archive.stat()
        except FileNotFoundError:
            return False
        with self._connect() as db:
            row = db.execute("SELECT archive_mtime_ns,archive_size FROM galleries WHERE id=?", (int(archive.stem),)).fetchone()
        return row is None or row["archive_mtime_ns"] != stat.st_mtime_ns or row["archive_size"] != stat.st_size

    def archive_stamps(self) -> dict[int, tuple[int, int]]:
        with self._connect() as db:
            rows = db.execute("SELECT id,archive_mtime_ns,archive_size FROM galleries").fetchall()
        return {row["id"]: (row["archive_mtime_ns"], row["archive_size"]) for row in rows}

    def index_archive(
        self,
        archive: Path,
        *,
        metadata: dict[str, object] | None = None,
        source_name: str = "archive",
    ) -> str:
        gallery_id = archive.stem
        if not archive.is_file() or not gallery_id.isdigit():
            raise FileNotFoundError(archive)
        source = self.archive_source(archive)
        parsed = metadata or parse_gallery_metadata(source, gallery_id)
        complete = parsed is not None
        if parsed is not None:
            parsed = dict(parsed)
            cover_match = ARCHIVE_COVER_RE.search(source)
            if cover_match and not parsed.get("cover_url"):
                cover_url = cover_match.group("url")
                parsed["cover_url"] = f"https:{cover_url}" if cover_url.startswith("//") else cover_url
        else:
            parsed = fallback_gallery_metadata(source, gallery_id)
        self.upsert_gallery(archive, parsed, complete=complete, source=source_name if complete else "fallback")
        return "complete" if complete else "pending"

    def upsert_gallery(
        self,
        archive: Path,
        metadata: dict[str, object],
        *,
        complete: bool,
        source: str,
    ) -> None:
        gallery_id = int(archive.stem)
        if str(metadata.get("id")) != str(gallery_id):
            raise ValueError("metadata gallery id does not match archive")
        stat = archive.stat()
        title = metadata.get("title") if isinstance(metadata.get("title"), dict) else {}
        cover = metadata.get("cover") if isinstance(metadata.get("cover"), dict) else {}
        cover_url = str(metadata.get("cover_url") or "")
        cover_path = str(cover.get("path") or "")
        if not cover_url and cover_path:
            cover_url = f"https://t.nhentai.net/{cover_path}"
        tags = metadata.get("tags") if isinstance(metadata.get("tags"), list) else []
        with self._write_lock, self._connect() as db:
            db.execute(
                """INSERT INTO galleries(
                    id,media_id,title_english,title_japanese,title_pretty,cover_url,cover_path,
                    downloaded_at,archive_mtime_ns,archive_size,metadata_status,metadata_source,indexed_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                    media_id=excluded.media_id,title_english=excluded.title_english,
                    title_japanese=excluded.title_japanese,title_pretty=excluded.title_pretty,
                    cover_url=excluded.cover_url,cover_path=excluded.cover_path,
                    downloaded_at=excluded.downloaded_at,archive_mtime_ns=excluded.archive_mtime_ns,
                    archive_size=excluded.archive_size,metadata_status=excluded.metadata_status,
                    metadata_source=excluded.metadata_source,indexed_at=excluded.indexed_at""",
                (
                    gallery_id,
                    int(metadata["media_id"]) if str(metadata.get("media_id", "")).isdigit() else None,
                    str(title.get("english") or ""),
                    str(title.get("japanese") or ""),
                    str(title.get("pretty") or title.get("english") or f"Gallery {gallery_id}"),
                    cover_url,
                    cover_path,
                    stat.st_mtime,
                    stat.st_mtime_ns,
                    stat.st_size,
                    "complete" if complete else "pending",
                    source,
                    time.time(),
                ),
            )
            db.execute("DELETE FROM gallery_taxonomies WHERE gallery_id=?", (gallery_id,))
            for position, tag in enumerate(tags):
                if not isinstance(tag, dict) or str(tag.get("type")) not in GALLERY_TYPES:
                    continue
                if not str(tag.get("id", "")).isdigit() or not tag.get("name"):
                    continue
                taxonomy_id = int(tag["id"])
                taxonomy_type = str(tag["type"])
                upstream_url = str(tag.get("url") or "")
                slug = str(tag.get("slug") or "")
                if not slug and upstream_url:
                    parts = [part for part in upstream_url.split("/") if part]
                    slug = parts[1] if len(parts) > 1 else ""
                if not slug:
                    slug = re.sub(r"[^a-z0-9]+", "-", str(tag["name"]).lower()).strip("-")
                canonical_id = self._resolve_taxonomy(
                    db, taxonomy_id, taxonomy_type, str(tag["name"]), slug, upstream_url
                )
                db.execute(
                    """INSERT INTO gallery_taxonomies(
                    gallery_id,taxonomy_id,taxonomy_type,source_taxonomy_id,position
                    ) VALUES(?,?,?,?,?)""",
                    (gallery_id, canonical_id, taxonomy_type, taxonomy_id, position),
                )
            db.execute(
                "DELETE FROM taxonomies WHERE NOT EXISTS (SELECT 1 FROM gallery_taxonomies gt WHERE gt.taxonomy_id=taxonomies.id AND gt.taxonomy_type=taxonomies.type)"
            )

    @staticmethod
    def _resolve_taxonomy(
        db: sqlite3.Connection,
        upstream_id: int,
        taxonomy_type: str,
        name: str,
        slug: str,
        upstream_url: str,
    ) -> int:
        """Resolve unstable upstream IDs to one local category identified by type+slug."""

        slug_row = db.execute(
            "SELECT id FROM taxonomies WHERE type=? AND slug=?", (taxonomy_type, slug)
        ).fetchone()
        id_row = db.execute(
            "SELECT id FROM taxonomies WHERE type=? AND id=?", (taxonomy_type, upstream_id)
        ).fetchone()

        if slug_row is not None:
            canonical_id = int(slug_row["id"])
            if id_row is not None and int(id_row["id"]) != canonical_id:
                old_id = int(id_row["id"])
                db.execute(
                    """INSERT OR IGNORE INTO gallery_taxonomies(
                    gallery_id,taxonomy_id,taxonomy_type,source_taxonomy_id,position
                    ) SELECT gallery_id,?,taxonomy_type,source_taxonomy_id,position
                    FROM gallery_taxonomies WHERE taxonomy_id=? AND taxonomy_type=?""",
                    (canonical_id, old_id, taxonomy_type),
                )
                db.execute(
                    "DELETE FROM gallery_taxonomies WHERE taxonomy_id=? AND taxonomy_type=?",
                    (old_id, taxonomy_type),
                )
                db.execute(
                    "UPDATE taxonomy_aliases SET canonical_id=? WHERE canonical_id=? AND taxonomy_type=?",
                    (canonical_id, old_id, taxonomy_type),
                )
                db.execute("DELETE FROM taxonomies WHERE id=? AND type=?", (old_id, taxonomy_type))
            db.execute(
                "UPDATE taxonomies SET name=?,upstream_url=? WHERE id=? AND type=?",
                (name, upstream_url, canonical_id, taxonomy_type),
            )
        elif id_row is not None:
            canonical_id = upstream_id
            db.execute(
                "UPDATE taxonomies SET name=?,slug=?,upstream_url=? WHERE id=? AND type=?",
                (name, slug, upstream_url, canonical_id, taxonomy_type),
            )
        else:
            canonical_id = upstream_id
            db.execute(
                "INSERT INTO taxonomies(id,type,name,slug,upstream_url) VALUES(?,?,?,?,?)",
                (canonical_id, taxonomy_type, name, slug, upstream_url),
            )

        db.execute(
            """INSERT INTO taxonomy_aliases(taxonomy_type,upstream_id,canonical_id) VALUES(?,?,?)
            ON CONFLICT(taxonomy_type,upstream_id) DO UPDATE SET canonical_id=excluded.canonical_id""",
            (taxonomy_type, upstream_id, canonical_id),
        )
        return canonical_id

    def delete_gallery(self, gallery_id: str) -> None:
        with self._write_lock, self._connect() as db:
            db.execute("DELETE FROM galleries WHERE id=?", (int(gallery_id),))
            db.execute(
                "DELETE FROM taxonomies WHERE NOT EXISTS (SELECT 1 FROM gallery_taxonomies gt WHERE gt.taxonomy_id=taxonomies.id AND gt.taxonomy_type=taxonomies.type)"
            )

    def pending_ids(self) -> list[str]:
        with self._connect() as db:
            rows = db.execute("SELECT id FROM galleries WHERE metadata_status='pending' ORDER BY id DESC").fetchall()
        return [str(row["id"]) for row in rows]

    def reconcile_missing_archives(self) -> None:
        archive_ids = {int(path.stem) for path in self.storage_dir.glob("*.cbz") if path.stem.isdigit()}
        with self._write_lock, self._connect() as db:
            rows = db.execute("SELECT id FROM galleries").fetchall()
            for row in rows:
                if row["id"] not in archive_ids:
                    db.execute("DELETE FROM galleries WHERE id=?", (row["id"],))
            db.execute(
                "DELETE FROM taxonomies WHERE NOT EXISTS (SELECT 1 FROM gallery_taxonomies gt WHERE gt.taxonomy_id=taxonomies.id AND gt.taxonomy_type=taxonomies.type)"
            )

    def gallery(self, gallery_id: str) -> dict[str, object] | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM galleries WHERE id=?", (int(gallery_id),)).fetchone()
            if row is None:
                return None
            record = dict(row)
            record["tags"] = [
                dict(item)
                for item in db.execute(
                    """SELECT COALESCE(gt.source_taxonomy_id,t.id) AS id,t.type,t.name,t.slug,t.upstream_url,
                    (SELECT count(*) FROM gallery_taxonomies totals
                     WHERE totals.taxonomy_id=t.id AND totals.taxonomy_type=t.type) AS local_count
                    FROM gallery_taxonomies gt JOIN taxonomies t
                    ON t.id=gt.taxonomy_id AND t.type=gt.taxonomy_type
                    WHERE gt.gallery_id=? ORDER BY gt.position""",
                    (int(gallery_id),),
                )
            ]
        record["id"] = str(record["id"])
        record["title"] = record["title_pretty"] or record["title_english"] or f"Gallery {gallery_id}"
        return record

    def downloaded(self, *, page: int = 1, per_page: int = 25) -> tuple[list[dict[str, object]], int]:
        return self._paged("SELECT * FROM galleries ORDER BY downloaded_at DESC,id DESC", (), page, per_page)

    def random(self, limit: int = 5) -> list[dict[str, object]]:
        with self._connect() as db:
            return [self._record(row) for row in db.execute("SELECT * FROM galleries ORDER BY random() LIMIT ?", (limit,))]

    def search(self, query: str, *, page: int = 1, per_page: int = 25) -> tuple[list[dict[str, object]], int]:
        query = query.strip()
        if not query:
            return self._paged("SELECT * FROM galleries ORDER BY id DESC", (), page, per_page)
        escaped = query.replace('"', '""')
        if len(query) >= 3:
            sql = "SELECT g.* FROM gallery_titles_fts f JOIN galleries g ON g.id=f.rowid WHERE gallery_titles_fts MATCH ? ORDER BY g.id DESC"
            try:
                return self._paged(sql, (f'"{escaped}"',), page, per_page)
            except sqlite3.OperationalError:
                pass
        pattern = f"%{query}%"
        return self._paged(
            """SELECT * FROM galleries WHERE title_english LIKE ? COLLATE NOCASE
            OR title_japanese LIKE ? OR title_pretty LIKE ? COLLATE NOCASE ORDER BY id DESC""",
            (pattern, pattern, pattern), page, per_page,
        )

    def taxonomy(
        self, taxonomy_type: str, slug: str, *, page: int = 1, per_page: int = 25
    ) -> tuple[str | None, list[dict[str, object]], int]:
        if taxonomy_type not in GALLERY_TYPES:
            return None, [], 0
        with self._connect() as db:
            taxonomy = db.execute("SELECT id,name FROM taxonomies WHERE type=? AND slug=?", (taxonomy_type, slug)).fetchone()
        if taxonomy is None:
            return None, [], 0
        sql = """SELECT g.* FROM gallery_taxonomies gt JOIN galleries g ON g.id=gt.gallery_id
        WHERE gt.taxonomy_type=? AND gt.taxonomy_id=? ORDER BY g.id DESC"""
        records, total = self._paged(sql, (taxonomy_type, taxonomy["id"]), page, per_page)
        return str(taxonomy["name"]), records, total

    def _paged(
        self, sql: str, params: tuple[object, ...], page: int, per_page: int
    ) -> tuple[list[dict[str, object]], int]:
        page = max(1, page)
        with self._connect() as db:
            total = db.execute(f"SELECT count(*) FROM ({sql})", params).fetchone()[0]
            rows = db.execute(f"{sql} LIMIT ? OFFSET ?", (*params, per_page, (page - 1) * per_page)).fetchall()
        return [self._record(row) for row in rows], int(total)

    @staticmethod
    def _record(row: sqlite3.Row) -> dict[str, object]:
        record = dict(row)
        record["id"] = str(record["id"])
        record["title"] = record["title_pretty"] or record["title_english"] or f"Gallery {record['id']}"
        return record


class LibraryIndexer:
    """Non-blocking, resumable archive scanner with rate-limited remote repair."""

    def __init__(
        self,
        database: LibraryDatabase,
        fetch_metadata: Callable[[str], dict[str, object]],
        *,
        autostart: bool = True,
        request_interval: float = 1.0,
    ) -> None:
        self.database = database
        self.fetch_metadata = fetch_metadata
        self.request_interval = request_interval
        self.stop_event = threading.Event()
        self.wake_event = threading.Event()
        self.worker: threading.Thread | None = None
        if autostart:
            self.start()

    def start(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        self.worker = threading.Thread(target=self._loop, name="nh-library-indexer", daemon=True)
        self.worker.start()

    def wake(self) -> None:
        self.wake_event.set()

    def index_now(self, *, repair_remote: bool = True) -> None:
        self.database.reconcile_missing_archives()
        known = self.database.archive_stamps()
        for archive in sorted(self.database.storage_dir.glob("*.cbz"), key=lambda item: int(item.stem) if item.stem.isdigit() else -1, reverse=True):
            if not archive.stem.isdigit():
                continue
            stat = archive.stat()
            if known.get(int(archive.stem)) != (stat.st_mtime_ns, stat.st_size):
                try:
                    self.database.index_archive(archive)
                except Exception as exc:  # noqa: BLE001 - one archive must not block the remaining library.
                    print(f"library index failed for {archive.stem}: {type(exc).__name__}: {exc}", flush=True)
        if repair_remote:
            for gallery_id in self.database.pending_ids():
                archive = self.database.storage_dir / f"{gallery_id}.cbz"
                if not archive.exists():
                    continue
                try:
                    metadata = self.fetch_metadata(gallery_id)
                    self.database.index_archive(archive, metadata=metadata, source_name="upstream")
                except Exception as exc:  # noqa: BLE001 - leave the row resumably pending.
                    print(f"library index pending for {gallery_id}: {exc}", flush=True)
                if self.stop_event.wait(self.request_interval):
                    return

    def _loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                self.index_now()
            except Exception as exc:  # noqa: BLE001 - keep the server available if indexing fails.
                if not self.database.storage_dir.exists():
                    return
                print(f"library indexer failed: {exc}", flush=True)
            self.wake_event.wait(300)
            self.wake_event.clear()
