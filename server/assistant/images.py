"""Bounded, deterministic CBZ sampling for explicitly authorized visual jobs."""
import base64
import hashlib
import io
from pathlib import Path, PurePosixPath
import warnings
import zipfile

from PIL import Image, ImageOps, UnidentifiedImageError

SUFFIXES = {'.jpg', '.jpeg', '.png', '.gif', '.webp'}
SAMPLER_VERSION = 'sparse-v1'
MAX_MEMBER = 64 * 1024 * 1024
MAX_PIXELS = 16_000_000
Image.MAX_IMAGE_PIXELS = MAX_PIXELS


class ImageSampleError(ValueError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def source_token(path):
    stamp = path.stat()
    return f'{stamp.st_size}:{stamp.st_mtime_ns}'


def _safe_member(info):
    name = info.filename
    parts = PurePosixPath(name).parts
    return (not info.is_dir() and not name.startswith(('/', '\\')) and '\\' not in name
            and all(part not in ('', '.', '..') for part in parts)
            and not (info.flag_bits & 1) and info.compress_type in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
            and PurePosixPath(name).suffix.lower() in SUFFIXES
            and PurePosixPath(name).stem.isdigit())


def image_members(archive):
    with zipfile.ZipFile(archive) as cbz:
        members = []
        for info in cbz.infolist():
            if info.is_dir() or PurePosixPath(info.filename).suffix.lower() not in SUFFIXES:
                continue
            if not _safe_member(info):
                raise ImageSampleError('image_decode_failed')
            if info.file_size > MAX_MEMBER or (info.file_size and info.file_size / max(1, info.compress_size) > 200):
                raise ImageSampleError('image_decode_failed')
            members.append(info)
    members.sort(key=lambda i: (int(PurePosixPath(i.filename).stem), PurePosixPath(i.filename).name, i.filename))
    return members


def matches_sample(archive, evidence):
    """Detect a selected-page replacement even when size and mtime were preserved."""
    try:
        members = image_members(archive)
        recorded = evidence.get('members', [])
        if len(members) != evidence.get('total_pages') or not recorded:
            return False
        with zipfile.ZipFile(archive) as cbz:
            for item in recorded:
                page = item['page']
                if type(page) is not int or not 1 <= page <= len(members):
                    return False
                info = members[page - 1]
                if info.filename != item['member']:
                    return False
                with cbz.open(info) as handle:
                    if hashlib.sha256(handle.read(MAX_MEMBER + 1)).hexdigest() != item['sha256']:
                        return False
        return True
    except (OSError, zipfile.BadZipFile, KeyError, TypeError, ValueError, RuntimeError):
        return False


def positions(count):
    if count <= 0:
        return []
    wanted = 1 if count == 1 else min(count, 4 if count <= 40 else 5 if count <= 150 else 6)
    if wanted == 1:
        return [1]
    return sorted({1 + round(i * (count - 1) / (wanted - 1)) for i in range(wanted)})


def _jpeg(raw, edge, byte_limit):
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as loaded:
                if loaded.width * loaded.height > MAX_PIXELS:
                    raise ImageSampleError('image_decode_failed')
                loaded.seek(0)
                image = ImageOps.exif_transpose(loaded).convert('RGB')
                image.thumbnail((edge, edge), Image.Resampling.LANCZOS)
                for quality in (82, 68):
                    output = io.BytesIO()
                    image.save(output, 'JPEG', quality=quality, optimize=True)
                    if output.tell() <= byte_limit:
                        return output.getvalue()
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ImageSampleError('image_decode_failed') from exc
    raise ImageSampleError('payload_too_large')


def sample(archive, *, edge=896, byte_limit=524288, image_limit=6, request_limit=5242880):
    """Return transient JPEG bytes and safe provenance; caller must discard bytes."""
    archive = Path(archive)
    before = source_token(archive)
    members = image_members(archive)
    selected = positions(len(members))[:image_limit]
    if not selected:
        raise ImageSampleError('no_readable_pages')
    pages, total_bytes = [], 0
    try:
        with zipfile.ZipFile(archive) as cbz:
            for page in selected:
                info = members[page - 1]
                with cbz.open(info) as handle:
                    raw = handle.read(MAX_MEMBER + 1)
                if len(raw) > MAX_MEMBER:
                    raise ImageSampleError('image_decode_failed')
                encoded = _jpeg(raw, edge, byte_limit)
                total_bytes += 4 * ((len(encoded) + 2) // 3) + 256
                if total_bytes > request_limit:
                    raise ImageSampleError('payload_too_large')
                pages.append(dict(page=page, member=info.filename,
                                  member_sha256=hashlib.sha256(raw).hexdigest(), jpeg=encoded))
    except (zipfile.BadZipFile, RuntimeError, OSError, EOFError) as exc:
        raise ImageSampleError('image_decode_failed') from exc
    if source_token(archive) != before:
        raise ImageSampleError('source_changed')
    return dict(source_fingerprint=before, total_pages=len(members), pages=pages,
                sampler_version=SAMPLER_VERSION)


def messages_for_sample(sampled, prompt):
    parts = [{'type': 'text', 'text': prompt}]
    for item in sampled['pages']:
        parts.extend(({'type': 'text', 'text': f"Sampled reader page {item['page']} of {sampled['total_pages']}:"},
                      {'type': 'image_url', 'image_url': {'url': 'data:image/jpeg;base64,' + base64.b64encode(item['jpeg']).decode('ascii')}}))
    return [{'role': 'user', 'content': parts}]


def safe_evidence(sampled):
    return dict(pages=[p['page'] for p in sampled['pages']], total_pages=sampled['total_pages'],
                members=[{'page': p['page'], 'member': p['member'], 'sha256': p['member_sha256']} for p in sampled['pages']],
                sampler_version=sampled['sampler_version'], coverage='sampled')
