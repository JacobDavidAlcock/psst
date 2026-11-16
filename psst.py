#!/usr/bin/env python3
"""psst - a friendlier `python -m http.server` for moving files between devices.

Serve a folder over HTTP with a web UI for browsing, previewing (images, text,
video, audio, PDF), uploading (drag & drop, folders, paste), downloading folders
as zip, and a shared text clipboard. Standard library only.

    python psst.py [DIR] [-p PORT] [--read-only] [--password PW]
"""

import argparse
import base64
import collections
import email.utils
import io
import json
import mimetypes
import os
import re
import secrets
import shutil
import socket
import struct
import sys
import threading
import time
import urllib.parse
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:  # optional: with Pillow installed, every image type gets a real thumbnail
    from PIL import Image, ImageOps
except ImportError:
    Image = None

__version__ = "0.2.0"

CHUNK = 1024 * 1024
PART_PREFIX = ".psst-upload-"
TRASH_DIR = ".psst-trash"
TRASH_DAYS = 7
STALE_PART_SECS = 2 * 86400
UPLOAD_ID = re.compile(r"[0-9a-f]{16,64}")
THUMB_SIZE = 400
API = "/__psst/"
MAX_CLIP_BYTES = 2 * 1024 * 1024
MAX_CLIP_ITEMS = 50

IMAGE_EXT = {"png", "jpg", "jpeg", "gif", "webp", "bmp", "svg", "ico", "avif", "apng"}
VIDEO_EXT = {"mp4", "webm", "mov", "m4v", "ogv", "mkv"}
AUDIO_EXT = {"mp3", "wav", "ogg", "oga", "m4a", "flac", "aac", "opus"}
ARCHIVE_EXT = {"zip", "tar", "gz", "tgz", "bz2", "xz", "7z", "rar", "zst", "iso", "dmg"}
TEXT_EXT = {
    "txt", "md", "markdown", "rst", "log", "csv", "tsv", "json", "jsonl", "yaml", "yml",
    "toml", "ini", "cfg", "conf", "env", "xml", "html", "htm", "css", "scss", "less",
    "js", "mjs", "cjs", "ts", "tsx", "jsx", "vue", "svelte", "py", "pyw", "rb", "php",
    "pl", "lua", "go", "rs", "java", "kt", "kts", "scala", "swift", "c", "h", "cc",
    "cpp", "cxx", "hpp", "cs", "fs", "m", "mm", "sh", "bash", "zsh", "fish", "ps1",
    "psm1", "bat", "cmd", "sql", "graphql", "proto", "tex", "r", "dart", "ex", "exs",
    "erl", "hs", "clj", "lisp", "el", "vim", "diff", "patch", "srt", "vtt", "gradle",
    "properties", "lock", "gitignore", "gitattributes", "editorconfig", "dockerignore",
}
TEXT_NAMES = {"makefile", "dockerfile", "readme", "license", "copying", "changelog",
              "authors", "procfile", "gemfile", "rakefile", "vagrantfile", "justfile"}

WIN_RESERVED = {"con", "prn", "aux", "nul"} | {f"com{i}" for i in range(1, 10)} | {f"lpt{i}" for i in range(1, 10)}


def file_ext(name):
    base = name.lower().rsplit("/", 1)[-1]
    if base.startswith(".") and base.count(".") == 1:
        return base[1:]
    return base.rsplit(".", 1)[-1] if "." in base else ""


def kind_of(name):
    ext = file_ext(name)
    if ext in IMAGE_EXT:
        return "image"
    if ext in VIDEO_EXT:
        return "video"
    if ext in AUDIO_EXT:
        return "audio"
    if ext == "pdf":
        return "pdf"
    if ext in ARCHIVE_EXT:
        return "archive"
    if ext in TEXT_EXT or name.lower() in TEXT_NAMES:
        return "text"
    return "file"


def content_type(path):
    name = os.path.basename(path)
    kind = kind_of(name)
    ext = file_ext(name)
    if kind == "text" and ext not in ("html", "htm", "css", "xml", "svg", "csv"):
        return "text/plain; charset=utf-8"
    ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
    if ctype.startswith("text/"):
        ctype += "; charset=utf-8"
    return ctype


def sanitize_part(part):
    """Make one path component safe to create on this OS. Returns None if unusable."""
    part = "".join("_" if (ord(c) < 32 or c in '/\\\0') else c for c in part)
    if os.name == "nt":
        part = "".join("_" if c in '<>:"|?*' else c for c in part).rstrip(" .")
        if part.split(".", 1)[0].lower() in WIN_RESERVED:
            part = "_" + part
    if part in ("", ".", ".."):
        return None
    return part[:255]


def unique_name(directory, name):
    if not os.path.exists(os.path.join(directory, name)):
        return name
    stem, ext = os.path.splitext(name)
    n = 1
    while os.path.exists(os.path.join(directory, f"{stem} ({n}){ext}")):
        n += 1
    return f"{stem} ({n}){ext}"


def content_disposition(name, attachment):
    ascii_name = name.encode("ascii", "replace").decode().replace('"', "_").replace("?", "_")
    quoted = urllib.parse.quote(name, safe="")
    kind = "attachment" if attachment else "inline"
    return f"{kind}; filename=\"{ascii_name}\"; filename*=UTF-8''{quoted}"


def parse_range(header, size):
    """Return (start, end) for a single byte range, None if unsatisfiable, or 'full' to ignore."""
    if not header.startswith("bytes=") or "," in header:
        return "full"
    spec = header[6:].strip()
    try:
        first, last = spec.split("-", 1)
        if first == "":
            n = int(last)
            if n <= 0:
                return None
            return max(0, size - n), size - 1
        start = int(first)
        end = int(last) if last else size - 1
    except ValueError:
        return "full"
    if start >= size or end < start:
        return None
    return start, min(end, size - 1)


# ---------------------------------------------------------------------------
# Shared clipboard (in memory)
# ---------------------------------------------------------------------------

class Clipboard:
    def __init__(self):
        self.lock = threading.Lock()
        self.items = []
        self.version = 0
        self.next_id = 1

    def snapshot(self):
        with self.lock:
            return self.version, list(self.items)

    def add(self, text, source):
        with self.lock:
            item = {"id": self.next_id, "text": text, "time": time.time(), "from": source}
            self.next_id += 1
            self.items.insert(0, item)
            del self.items[MAX_CLIP_ITEMS:]
            self.version += 1
            return item

    def remove(self, item_id=None):
        with self.lock:
            if item_id is None:
                self.items.clear()
            else:
                self.items = [i for i in self.items if i["id"] != item_id]
            self.version += 1


def is_hidden(name):
    return name.startswith(PART_PREFIX) or name == TRASH_DIR


class HTTPError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


# ---------------------------------------------------------------------------
# Thumbnails
# ---------------------------------------------------------------------------

def exif_thumbnail(path):
    """Return the small JPEG preview most cameras and phones embed in a photo, or None."""
    try:
        with open(path, "rb") as f:
            head = f.read(128 * 1024)
        if head[:2] != b"\xff\xd8":
            return None
        i = 2
        while i + 4 <= len(head):
            if head[i] != 0xFF:
                return None
            marker = head[i + 1]
            if marker in (0xDA, 0xD9):  # image data starts: no EXIF block
                return None
            seglen = struct.unpack(">H", head[i + 2:i + 4])[0]
            if marker == 0xE1 and head[i + 4:i + 10] == b"Exif\0\0":
                return _exif_ifd1_jpeg(head[i + 10:i + 2 + seglen])
            i += 2 + seglen
    except (OSError, struct.error, IndexError, ValueError):
        pass
    return None


def _exif_ifd1_jpeg(tiff):
    endian = {b"II": "<", b"MM": ">"}.get(tiff[:2])
    if not endian:
        return None
    u16 = lambda o: struct.unpack_from(endian + "H", tiff, o)[0]  # noqa: E731
    u32 = lambda o: struct.unpack_from(endian + "I", tiff, o)[0]  # noqa: E731

    def entries(off):
        n = u16(off)
        return [(u16(off + 2 + 12 * k), u16(off + 4 + 12 * k), off + 10 + 12 * k) for k in range(n)], u32(off + 2 + 12 * n)

    ifd0, ifd1 = entries(u32(4))
    orientation = next((u16(v) for tag, _, v in ifd0 if tag == 0x0112), 1)
    if not ifd1:
        return None
    start = length = None
    for tag, typ, v in entries(ifd1)[0]:
        val = u16(v) if typ == 3 else u32(v)
        if tag == 0x0201:
            start = val
        elif tag == 0x0202:
            length = val
    if not start or not length or tiff[start:start + 2] != b"\xff\xd8":
        return None
    thumb = tiff[start:start + length]
    if orientation != 1:
        # The embedded preview isn't rotated; give it its own EXIF orientation so browsers turn it.
        tiff_hdr = b"MM\x00\x2a" + struct.pack(">IH", 8, 1) + struct.pack(">HHIHH", 0x0112, 3, 1, orientation, 0) + b"\0\0\0\0"
        payload = b"Exif\0\0" + tiff_hdr
        thumb = b"\xff\xd8\xff\xe1" + struct.pack(">H", len(payload) + 2) + payload + thumb[2:]
    return thumb


def make_thumbnail(path):
    """Return (bytes, content type) for a small preview of an image, or None to use the original."""
    ext = file_ext(path)
    if Image is not None and ext not in ("svg", "gif", "ico") and os.path.getsize(path) > 150 * 1024:
        try:
            with Image.open(path) as im:
                im.draft("RGB", (THUMB_SIZE, THUMB_SIZE))  # decode big JPEGs at reduced size: much faster
                im = ImageOps.exif_transpose(im)
                im.thumbnail((THUMB_SIZE, THUMB_SIZE))
                buf = io.BytesIO()
                if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
                    im.convert("RGBA").save(buf, "PNG", optimize=True)
                    return buf.getvalue(), "image/png"
                im.convert("RGB").save(buf, "JPEG", quality=80)
                return buf.getvalue(), "image/jpeg"
        except Exception:
            pass
    if ext in ("jpg", "jpeg"):
        data = exif_thumbnail(path)
        if data:
            return data, "image/jpeg"
    return None


class LRUCache:
    def __init__(self, max_bytes):
        self.lock = threading.Lock()
        self.items = collections.OrderedDict()
        self.size = 0
        self.max_bytes = max_bytes

    def get(self, key):
        with self.lock:
            if key in self.items:
                self.items.move_to_end(key)
                return self.items[key]
        return None

    def put(self, key, value):
        with self.lock:
            if key in self.items:
                return
            self.items[key] = value
            self.size += len(value[0]) + 100
            while self.size > self.max_bytes and self.items:
                _, old = self.items.popitem(last=False)
                self.size -= len(old[0]) + 100


def purge_trash(root):
    """Permanently delete trash batches older than TRASH_DAYS."""
    tdir = os.path.join(root, TRASH_DIR)
    try:
        cutoff = time.time() - TRASH_DAYS * 86400
        with os.scandir(tdir) as it:
            for e in it:
                if e.stat().st_mtime < cutoff:
                    shutil.rmtree(e.path, ignore_errors=True)
        if not os.listdir(tdir):
            os.rmdir(tdir)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

class _Writer:
    """Non-seekable wrapper so zipfile streams straight to the socket."""

    def __init__(self, wfile):
        self.wfile = wfile

    def write(self, data):
        self.wfile.write(data)
        return len(data)

    def flush(self):
        pass


class Handler(BaseHTTPRequestHandler):
    server_version = "psst/" + __version__
    protocol_version = "HTTP/1.1"
    timeout = 300

    # -- plumbing ----------------------------------------------------------

    @property
    def cfg(self):
        return self.server.cfg

    def handle(self):
        try:
            super().handle()
        except (ConnectionError, TimeoutError):
            pass

    def log_message(self, fmt, *args):
        stamp = time.strftime("%H:%M:%S")
        sys.stderr.write(f"{stamp} {self.client_address[0]:<15} {fmt % args}\n")

    def log_request(self, code="-", size="-"):
        path = getattr(self, "path", "")
        if self.command == "GET" and path.startswith((API + "clip", API + "thumb", API + "upload", API + "info")):
            return
        if self.command == "PUT" and str(code) == "200":  # chunk of a resumable upload
            return
        if str(code) == "304":
            return
        self.log_message("%s %s %s", self.command, urllib.parse.unquote(path), code)

    def send_body(self, status, body, ctype, headers=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        headers = headers or {}
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if "Cache-Control" not in headers:
            self.send_header("Cache-Control", "no-store")
        for k, v in headers.items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_json(self, status, obj):
        self.send_body(status, json.dumps(obj), "application/json")

    def fail(self, status, message):
        if self.command in ("PUT", "POST"):
            self.close_connection = True  # request body may be unread
        self.send_json(status, {"error": message})

    def check_auth(self):
        if not self.cfg.password:
            return True
        header = self.headers.get("Authorization", "")
        if header.startswith("Basic "):
            try:
                _, _, pw = base64.b64decode(header[6:]).decode("utf-8").partition(":")
                if secrets.compare_digest(pw.encode(), self.cfg.password.encode()):
                    return True
            except (ValueError, UnicodeDecodeError):
                pass
        if self.command in ("PUT", "POST"):
            self.close_connection = True
        self.send_body(401, "Password required\n", "text/plain; charset=utf-8",
                       {"WWW-Authenticate": 'Basic realm="psst", charset="UTF-8"'})
        return False

    def url_parts(self, urlpath):
        path = urllib.parse.unquote(urlpath)
        return [p for p in path.split("/") if p not in ("", ".")]

    def within_root(self, fs_path):
        root = self.cfg.root
        real = os.path.normcase(os.path.realpath(fs_path))
        return real == os.path.normcase(root) or real.startswith(os.path.normcase(root) + os.sep)

    def resolve(self, urlpath):
        parts = self.url_parts(urlpath)
        for p in parts:
            if p == ".." or "\0" in p or (os.name == "nt" and (":" in p or "\\" in p)):
                return None
        fs = os.path.join(self.cfg.root, *parts)
        return fs if self.within_root(fs) else None

    def read_body(self, limit):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1
        if length < 0 or length > limit:
            return None
        return self.rfile.read(length)

    def copy_body(self, out, length):
        remaining = length
        while remaining:
            chunk = self.rfile.read(min(CHUNK, remaining))
            if not chunk:
                raise ConnectionError("client disconnected")
            out.write(chunk)
            remaining -= len(chunk)

    # -- GET / HEAD --------------------------------------------------------

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        if not self.check_auth():
            return
        url = urllib.parse.urlsplit(self.path)
        if url.path.startswith(API):
            try:
                return self.api_get(url)
            except HTTPError as e:
                return self.fail(e.status, e.message)
        query = urllib.parse.parse_qs(url.query, keep_blank_values=True)
        fs = self.resolve(url.path)
        if fs is None or not os.path.exists(fs):
            return self.not_found(url.path)
        if os.path.isdir(fs):
            if not url.path.endswith("/"):
                target = url.path + "/" + ("?" + url.query if url.query else "")
                return self.send_body(301, "", "text/plain", {"Location": target})
            if "zip" in query:
                return self.send_zip(self.zip_name(fs) + ".zip", [(fs, self.zip_name(fs))])
            if "json" in query:
                return self.send_listing(fs, url.path)
            if "text/html" in self.headers.get("Accept", ""):
                return self.send_body(200, APP_HTML, "text/html; charset=utf-8")
            return self.send_plain_listing(fs)
        return self.send_file(fs, download="dl" in query)

    def not_found(self, path):
        if "text/html" in self.headers.get("Accept", "") and path.endswith("/"):
            return self.send_body(404, APP_HTML, "text/html; charset=utf-8")
        self.send_body(404, "Not found\n", "text/plain; charset=utf-8")

    def list_dir(self, fs):
        entries = []
        now = time.time()
        with os.scandir(fs) as it:
            for e in it:
                if is_hidden(e.name):
                    if e.name.startswith(PART_PREFIX):
                        try:  # abandoned resumable uploads
                            if now - e.stat().st_mtime > STALE_PART_SECS:
                                os.remove(e.path)
                        except OSError:
                            pass
                    continue
                try:
                    is_dir = e.is_dir()
                    st = e.stat()
                except OSError:
                    continue
                entries.append({
                    "name": e.name,
                    "dir": is_dir,
                    "size": 0 if is_dir else st.st_size,
                    "mtime": st.st_mtime,
                    "kind": "dir" if is_dir else kind_of(e.name),
                })
        return entries

    def send_listing(self, fs, urlpath):
        try:
            entries = self.list_dir(fs)
        except OSError as e:
            return self.fail(403, str(e))
        self.send_json(200, {"path": urlpath, "readOnly": self.cfg.read_only, "entries": entries})

    def send_plain_listing(self, fs):
        try:
            entries = self.list_dir(fs)
        except OSError as e:
            return self.fail(403, str(e))
        entries.sort(key=lambda e: (not e["dir"], e["name"].lower()))
        lines = [e["name"] + ("/" if e["dir"] else "") for e in entries]
        self.send_body(200, "\n".join(lines) + "\n", "text/plain; charset=utf-8")

    def send_file(self, fs, download=False):
        try:
            f = open(fs, "rb")
        except OSError:
            return self.send_body(403, "Forbidden\n", "text/plain; charset=utf-8")
        with f:
            st = os.fstat(f.fileno())
            size = st.st_size
            etag = f'"{st.st_mtime_ns:x}-{size:x}"'
            last_mod = email.utils.formatdate(st.st_mtime, usegmt=True)
            if self.headers.get("If-None-Match") == etag:
                self.send_response(304)
                self.send_header("ETag", etag)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            start, end, status = 0, size - 1, 200
            rng = self.headers.get("Range")
            if_range = self.headers.get("If-Range")
            if rng and size > 0 and (not if_range or if_range in (etag, last_mod)):
                r = parse_range(rng, size)
                if r is None:
                    return self.send_body(416, "", "text/plain", {"Content-Range": f"bytes */{size}"})
                if r != "full":
                    start, end = r
                    status = 206
            length = max(0, end - start + 1)
            self.send_response(status)
            self.send_header("Content-Type", content_type(fs))
            self.send_header("Content-Length", str(length))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Last-Modified", last_mod)
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Disposition", content_disposition(os.path.basename(fs), download))
            if status == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            if self.command == "HEAD" or length == 0:
                return
            if hasattr(os, "sendfile"):
                self.connection.sendfile(f, start, length)
            else:
                f.seek(start)
                remaining = length
                while remaining:
                    chunk = f.read(min(CHUNK, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)

    def zip_name(self, fs):
        if os.path.normcase(os.path.realpath(fs)) == os.path.normcase(self.cfg.root):
            return os.path.basename(self.cfg.root) or "files"
        return os.path.basename(os.path.normpath(fs)) or "files"

    def send_zip(self, zip_name, entries):
        """Stream a zip of [(path, name inside zip)] without building it on disk first."""
        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Disposition", content_disposition(zip_name, True))
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        if self.command == "HEAD":
            return
        with zipfile.ZipFile(_Writer(self.wfile), "w", zipfile.ZIP_STORED, allowZip64=True) as zf:
            for fs, arc in entries:
                if not os.path.isdir(fs):
                    try:
                        zf.write(fs, arc)
                    except OSError:
                        pass
                    continue
                for dirpath, dirnames, filenames in os.walk(fs):
                    dirnames[:] = sorted(d for d in dirnames if not is_hidden(d))
                    rel = os.path.relpath(dirpath, fs)
                    arc_dir = arc if rel == "." else arc + "/" + rel.replace(os.sep, "/")
                    files = sorted(f for f in filenames if not is_hidden(f))
                    if not files and not dirnames:
                        zf.writestr(arc_dir + "/", b"")
                    for fn in files:
                        try:
                            zf.write(os.path.join(dirpath, fn), arc_dir + "/" + fn)
                        except OSError:
                            continue
        self.log_message("zip %s done", zip_name)

    # -- API ---------------------------------------------------------------

    def api_get(self, url):
        route = url.path[len(API):]
        query = urllib.parse.parse_qs(url.query)
        arg = lambda k: query.get(k, [""])[0]  # noqa: E731
        if route == "info":
            return self.send_json(200, {
                "version": __version__,
                "readOnly": self.cfg.read_only,
                "urls": self.cfg.urls,
                "hostname": socket.gethostname(),
                "thumbs": "pillow" if Image is not None else "exif",
            })
        if route == "clip":
            version, items = self.cfg.clipboard.snapshot()
            if arg("v") == str(version):
                return self.send_json(200, {"v": version})
            return self.send_json(200, {"v": version, "items": items})
        if route == "qr.svg":
            try:
                svg = qr_svg(qr_encode(arg("text").encode("utf-8")))
            except ValueError as e:
                raise HTTPError(400, str(e))
            return self.send_body(200, svg, "image/svg+xml")
        if route == "thumb":
            return self.send_thumb(arg("path"))
        if route == "upload":
            # How much of a resumable upload the server already has.
            upload_id = arg("id")
            done = self.server.finished.get(upload_id)
            if done:
                return self.send_json(200, done)
            target_dir, parts = self.upload_target(arg("target"))
            part = os.path.join(target_dir, PART_PREFIX + upload_id + ".part")
            have = os.path.getsize(part) if UPLOAD_ID.fullmatch(upload_id) and os.path.isfile(part) else 0
            return self.send_json(200, {"received": have})
        raise HTTPError(404, "unknown endpoint")

    def send_thumb(self, urlpath):
        fs = self.resolve(urlpath)
        if fs is None or not os.path.isfile(fs):
            raise HTTPError(404, "not found")
        st = os.stat(fs)
        key = (fs, st.st_mtime_ns, st.st_size)
        hit = self.server.thumbs.get(key)
        if hit is None:
            hit = make_thumbnail(fs) or (b"", "")
            self.server.thumbs.put(key, hit)
        data, ctype = hit
        if not data:
            return self.send_file(fs)
        etag = f'"t{st.st_mtime_ns:x}-{st.st_size:x}"'
        if self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_body(200, data, ctype, {"Cache-Control": "max-age=604800", "ETag": etag})

    def do_POST(self):
        if not self.check_auth():
            return
        url = urllib.parse.urlsplit(self.path)
        route = url.path[len(API):] if url.path.startswith(API) else None
        try:
            if route == "clip":
                return self.post_clip()
            if route == "zip":
                return self.post_zip()
            ops = {"mkdir": self.op_mkdir, "delete": self.op_delete, "restore": self.op_restore,
                   "rename": self.op_rename, "move": self.op_move}
            if route not in ops:
                raise HTTPError(404, "unknown endpoint")
            if self.cfg.read_only:
                raise HTTPError(403, "server is read-only")
            body = self.read_body(1024 * 1024)
            try:
                data = json.loads(body)
            except (TypeError, ValueError):
                data = None
            if not isinstance(data, dict):
                raise HTTPError(400, "expected a JSON object")
            result = ops[route](data)
            self.send_json(200, result)
        except HTTPError as e:
            self.fail(e.status, e.message)

    def post_clip(self):
        body = self.read_body(MAX_CLIP_BYTES)
        try:
            text = json.loads(body)["text"] if body else ""
        except (ValueError, KeyError, TypeError):
            text = ""
        if not isinstance(text, str) or not text.strip():
            raise HTTPError(400, "empty or oversized text")
        item = self.cfg.clipboard.add(text, self.client_address[0])
        self.log_message("clipboard + %d chars", len(text))
        self.send_json(201, item)

    def post_zip(self):
        """Zip several items from one folder. Sent as a form so the browser downloads the reply."""
        body = self.read_body(4 * 1024 * 1024)
        if body is None:
            raise HTTPError(400, "request too large")
        form = urllib.parse.parse_qs(body.decode("utf-8", "replace"))
        folder, items = self.op_items({"path": form.get("path", [""])[0], "names": form.get("name", [])})
        if len(items) == 1:
            name = items[0][0]
        else:
            name = f"{self.zip_name(folder)} ({len(items)} items)"
        self.send_zip(name + ".zip", [(fs, n) for n, fs in items])

    def op_folder(self, urlpath):
        folder = self.resolve(urlpath if isinstance(urlpath, str) else "")
        if folder is None or not os.path.isdir(folder):
            raise HTTPError(404, "folder not found")
        return folder

    def op_items(self, data):
        """Resolve {"path": folder URL, "names": [...]} to (folder, [(name, path)])."""
        folder = self.op_folder(data.get("path"))
        names = data.get("names")
        if not isinstance(names, list) or not names:
            raise HTTPError(400, "nothing selected")
        items = []
        for n in names:
            if (not isinstance(n, str) or n in ("", ".", "..") or is_hidden(n)
                    or any(c in n for c in "/\\\0")):
                raise HTTPError(400, f"invalid name: {n}")
            fs = os.path.join(folder, n)
            if not os.path.lexists(fs):
                raise HTTPError(404, f"not found: {n}")
            items.append((n, fs))
        return folder, items

    def op_mkdir(self, data):
        parent = self.op_folder(data.get("path"))
        name = sanitize_part(str(data.get("name", "")).strip())
        if name is None or is_hidden(name):
            raise HTTPError(400, "invalid folder name")
        target = os.path.join(parent, name)
        if os.path.exists(target):
            raise HTTPError(409, "already exists")
        try:
            os.mkdir(target)
        except OSError as e:
            raise HTTPError(500, str(e))
        self.log_message("mkdir %s", name)
        return {"name": name}

    def op_delete(self, data):
        """Move items into the hidden trash folder so they can be restored for TRASH_DAYS."""
        _, items = self.op_items(data)
        batch = time.strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(3)
        bdir = os.path.join(self.cfg.root, TRASH_DIR, batch)
        os.makedirs(bdir)
        moved, errors = [], []
        for n, fs in items:
            try:
                shutil.move(fs, os.path.join(bdir, n))
                moved.append(n)
            except OSError as e:
                errors.append(f"{n}: {e.strerror or e}")
        with open(os.path.join(bdir, ".psst-meta.json"), "w", encoding="utf-8") as f:
            json.dump({"path": data["path"], "names": moved}, f)
        self.log_message("delete %s", ", ".join(moved) or "-")
        purge_trash(self.cfg.root)
        if not moved:
            raise HTTPError(409, "; ".join(errors))
        return {"trash": batch, "count": len(moved), "errors": errors}

    def op_restore(self, data):
        batch = data.get("trash")
        if not isinstance(batch, str) or not re.fullmatch(r"[\w-]+", batch):
            raise HTTPError(400, "bad trash id")
        bdir = os.path.join(self.cfg.root, TRASH_DIR, batch)
        try:
            with open(os.path.join(bdir, ".psst-meta.json"), encoding="utf-8") as f:
                meta = json.load(f)
        except (OSError, ValueError):
            raise HTTPError(404, "already restored or emptied")
        folder = self.resolve(meta.get("path", "")) or self.cfg.root
        os.makedirs(folder, exist_ok=True)
        restored = []
        for n in meta.get("names", []):
            src = os.path.join(bdir, n)
            if os.path.lexists(src):
                with self.server.upload_lock:
                    final = unique_name(folder, n)
                    shutil.move(src, os.path.join(folder, final))
                restored.append(final)
        shutil.rmtree(bdir, ignore_errors=True)
        self.log_message("restore %s", ", ".join(restored) or "-")
        return {"names": restored}

    def op_rename(self, data):
        folder, items = self.op_items({"path": data.get("path"), "names": [data.get("from")]})
        old, src = items[0]
        new = sanitize_part(str(data.get("to", "")).strip())
        if new is None or is_hidden(new):
            raise HTTPError(400, "invalid name")
        dest = os.path.join(folder, new)
        if os.path.lexists(dest) and os.path.normcase(dest) != os.path.normcase(src):
            raise HTTPError(409, f"{new} already exists")
        try:
            os.rename(src, dest)
        except OSError as e:
            raise HTTPError(409, e.strerror or str(e))
        self.log_message("rename %s -> %s", old, new)
        return {"name": new}

    def op_move(self, data):
        folder, items = self.op_items(data)
        dest = self.op_folder(data.get("dest"))
        real_dest = os.path.normcase(os.path.realpath(dest))
        if real_dest == os.path.normcase(os.path.realpath(folder)):
            raise HTTPError(400, "already in that folder")
        moved, errors = [], []
        for n, fs in items:
            real_src = os.path.normcase(os.path.realpath(fs))
            if real_dest == real_src or real_dest.startswith(real_src + os.sep):
                errors.append(f"can't move {n} into itself")
                continue
            try:
                with self.server.upload_lock:
                    final = unique_name(dest, n)
                    shutil.move(fs, os.path.join(dest, final))
                moved.append(final)
            except OSError as e:
                errors.append(f"{n}: {e.strerror or e}")
        self.log_message("move %s", ", ".join(moved) or "-")
        if not moved:
            raise HTTPError(409, "; ".join(errors))
        return {"names": moved, "errors": errors}

    def do_DELETE(self):
        if not self.check_auth():
            return
        url = urllib.parse.urlsplit(self.path)
        if url.path == API + "clip":
            self.cfg.clipboard.remove()
            return self.send_json(200, {"ok": True})
        if url.path.startswith(API + "clip/"):
            try:
                self.cfg.clipboard.remove(int(url.path.rsplit("/", 1)[1]))
            except ValueError:
                return self.fail(400, "bad id")
            return self.send_json(200, {"ok": True})
        self.fail(405, "use POST /__psst/delete")

    # -- uploads -----------------------------------------------------------

    def upload_target(self, urlpath):
        """Map an upload URL to (folder, sanitized path parts). The folder may not exist yet."""
        raw_parts = self.url_parts(urlpath)
        if not raw_parts or urlpath.endswith("/"):
            raise HTTPError(400, "upload URL must end with a file name")
        parts = [sanitize_part(p) for p in raw_parts]
        if None in parts or any(is_hidden(p) for p in parts):
            raise HTTPError(400, "invalid path")
        target_dir = os.path.join(self.cfg.root, *parts[:-1])
        if not self.within_root(target_dir):
            raise HTTPError(403, "outside shared folder")
        return target_dir, parts

    def finalize_upload(self, tmp, target_dir, name):
        with self.server.upload_lock:
            final = unique_name(target_dir, name)
            os.replace(tmp, os.path.join(target_dir, final))
        return final

    def do_PUT(self):
        """Upload the request body to the URL path. Works with `curl -T file URL`.

        Resumable uploads send the file in pieces with X-Psst-Upload (an id the
        client picks), X-Psst-Offset and X-Psst-Total headers.
        """
        if not self.check_auth():
            return
        try:
            if self.cfg.read_only:
                raise HTTPError(403, "server is read-only")
            url = urllib.parse.urlsplit(self.path)
            if url.path.startswith(API):
                raise HTTPError(404, "unknown endpoint")
            try:
                length = int(self.headers["Content-Length"])
            except (TypeError, ValueError):
                raise HTTPError(411, "Content-Length required")
            target_dir, parts = self.upload_target(url.path)
            try:
                os.makedirs(target_dir, exist_ok=True)
            except OSError as e:
                raise HTTPError(409, f"cannot create folder: {e}")
            upload_id = self.headers.get("X-Psst-Upload")
            if upload_id is not None:
                return self.put_chunk(target_dir, parts, upload_id, length)
        except HTTPError as e:
            return self.fail(e.status, e.message)

        tmp = os.path.join(target_dir, PART_PREFIX + secrets.token_hex(6) + ".part")
        started = time.time()
        try:
            with open(tmp, "wb") as out:
                self.copy_body(out, length)
            final = self.finalize_upload(tmp, target_dir, parts[-1])
        except BaseException:
            try:
                os.remove(tmp)
            except OSError:
                pass
            self.close_connection = True
            raise
        self.uploaded(parts, final, length, started)
        self.send_json(201, {"name": final, "path": "/".join(parts[:-1] + [final]), "size": length})

    def put_chunk(self, target_dir, parts, upload_id, length):
        try:
            offset = int(self.headers["X-Psst-Offset"])
            total = int(self.headers["X-Psst-Total"])
        except (TypeError, ValueError):
            raise HTTPError(400, "missing X-Psst-Offset or X-Psst-Total")
        if not UPLOAD_ID.fullmatch(upload_id) or offset < 0 or offset + length > total:
            raise HTTPError(400, "bad upload chunk")
        done = self.server.finished.get(upload_id)
        if done:  # finished earlier, but the client never saw the reply
            self.close_connection = True
            return self.send_json(201, done)
        part = os.path.join(target_dir, PART_PREFIX + upload_id + ".part")
        with self.server.part_lock(part):
            have = os.path.getsize(part) if os.path.exists(part) else 0
            if offset != have:
                self.close_connection = True
                return self.send_json(409, {"received": have})
            if offset == 0:
                self.server.upload_started[upload_id] = time.time()
            # If the connection drops mid-chunk, the bytes already written stay in the
            # .part file and the client resumes from there.
            with open(part, "ab") as out:
                self.copy_body(out, length)
            have += length
            if have < total:
                return self.send_json(200, {"received": have})
            final = self.finalize_upload(part, target_dir, parts[-1])
        result = {"name": final, "path": "/".join(parts[:-1] + [final]), "size": total,
                  "received": total, "done": True}
        self.server.remember_finished(upload_id, result)
        self.uploaded(parts, final, total, self.server.upload_started.pop(upload_id, time.time()))
        self.send_json(201, result)

    def uploaded(self, parts, final, size, started):
        secs = max(time.time() - started, 0.001)
        rel = "/".join(parts[:-1] + [final])
        self.log_message("upload %s (%s, %s/s)", rel, human_size(size), human_size(size / secs))


def human_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = os.name != "nt"  # on Windows this would allow two servers on one port
    request_queue_size = 64

    def __init__(self, addr, cfg):
        self.cfg = cfg
        self.upload_lock = threading.Lock()
        self.part_locks = {}
        self.finished = collections.OrderedDict()
        self.upload_started = {}
        self.thumbs = LRUCache(64 * 1024 * 1024)
        super().__init__(addr, Handler)

    def part_lock(self, key):
        with self.upload_lock:
            return self.part_locks.setdefault(key, threading.Lock())

    def remember_finished(self, upload_id, result):
        with self.upload_lock:
            self.finished[upload_id] = result
            while len(self.finished) > 2000:
                self.finished.popitem(last=False)


# ---------------------------------------------------------------------------
# QR code encoder (byte mode, ECC level M). Based on the algorithm in
# Project Nayuki's QR Code generator (MIT).
# ---------------------------------------------------------------------------

_ECC_PER_BLOCK_M = [-1, 10, 16, 26, 18, 24, 16, 18, 22, 22, 26, 30, 22, 22, 24, 24, 28, 28, 26, 26,
                    26, 26, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28]
_BLOCKS_M = [-1, 1, 1, 1, 2, 2, 4, 4, 4, 5, 5, 5, 8, 9, 9, 10, 10, 11, 13, 14, 16, 17, 17, 18, 20,
             21, 23, 25, 26, 28, 29, 31, 33, 35, 37, 38, 40, 43, 45, 47, 49]
_FORMAT_M = 0


def _gf_mul(x, y):
    z = 0
    for i in reversed(range(8)):
        z = (z << 1) ^ ((z >> 7) * 0x11D)
        z ^= ((y >> i) & 1) * x
    return z


def _rs_divisor(degree):
    result = [0] * (degree - 1) + [1]
    root = 1
    for _ in range(degree):
        for j in range(degree):
            result[j] = _gf_mul(result[j], root)
            if j + 1 < degree:
                result[j] ^= result[j + 1]
        root = _gf_mul(root, 0x02)
    return result


def _rs_remainder(data, divisor):
    result = [0] * len(divisor)
    for b in data:
        factor = b ^ result.pop(0)
        result.append(0)
        for i, coef in enumerate(divisor):
            result[i] ^= _gf_mul(coef, factor)
    return result


def _raw_modules(ver):
    result = (16 * ver + 128) * ver + 64
    if ver >= 2:
        n = ver // 7 + 2
        result -= (25 * n - 10) * n - 55
        if ver >= 7:
            result -= 36
    return result


def _data_codewords(ver):
    return _raw_modules(ver) // 8 - _ECC_PER_BLOCK_M[ver] * _BLOCKS_M[ver]


def _alignment_positions(ver):
    if ver == 1:
        return []
    n = ver // 7 + 2
    step = (ver * 8 + n * 3 + 5) // (n * 4 - 4) * 2
    size = ver * 4 + 17
    result = [size - 7 - i * step for i in range(n - 1)] + [6]
    return list(reversed(result))


def qr_encode(data, version=None, mask=None):
    """Return a square matrix (list of lists of bool, True = dark) encoding `data` bytes."""
    for ver in range(1, 41) if version is None else [version]:
        cc_bits = 8 if ver < 10 else 16
        if 4 + cc_bits + 8 * len(data) <= _data_codewords(ver) * 8:
            break
    else:
        raise ValueError("data too long for a QR code")

    # Data bits
    bits = []

    def put(val, n):
        bits.extend((val >> i) & 1 for i in reversed(range(n)))

    put(0b0100, 4)
    put(len(data), cc_bits)
    for b in data:
        put(b, 8)
    capacity = _data_codewords(ver) * 8
    put(0, min(4, capacity - len(bits)))
    put(0, -len(bits) % 8)
    pad = 0xEC
    while len(bits) < capacity:
        put(pad, 8)
        pad ^= 0xEC ^ 0x11
    codewords = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]

    # Error correction + interleaving
    num_blocks = _BLOCKS_M[ver]
    ecc_len = _ECC_PER_BLOCK_M[ver]
    raw = _raw_modules(ver) // 8
    num_short = num_blocks - raw % num_blocks
    short_len = raw // num_blocks
    divisor = _rs_divisor(ecc_len)
    blocks, k = [], 0
    for i in range(num_blocks):
        dat = codewords[k:k + short_len - ecc_len + (0 if i < num_short else 1)]
        k += len(dat)
        ecc = _rs_remainder(dat, divisor)
        if i < num_short:
            dat = dat + [0]
        blocks.append(dat + ecc)
    final = []
    for i in range(len(blocks[0])):
        for j, blk in enumerate(blocks):
            if i != short_len - ecc_len or j >= num_short:
                final.append(blk[i])

    # Function patterns
    size = ver * 4 + 17
    modules = [[False] * size for _ in range(size)]
    is_func = [[False] * size for _ in range(size)]

    def setf(x, y, dark):
        modules[y][x] = dark
        is_func[y][x] = True

    for i in range(size):
        setf(6, i, i % 2 == 0)
        setf(i, 6, i % 2 == 0)
    for cx, cy in ((3, 3), (size - 4, 3), (3, size - 4)):
        for dy in range(-4, 5):
            for dx in range(-4, 5):
                x, y = cx + dx, cy + dy
                if 0 <= x < size and 0 <= y < size:
                    setf(x, y, max(abs(dx), abs(dy)) not in (2, 4))
    pos = _alignment_positions(ver)
    last = len(pos) - 1
    for i, ax in enumerate(pos):
        for j, ay in enumerate(pos):
            if (i == 0 and j == 0) or (i == 0 and j == last) or (i == last and j == 0):
                continue
            for dy in range(-2, 3):
                for dx in range(-2, 3):
                    setf(ax + dx, ay + dy, max(abs(dx), abs(dy)) != 1)

    def draw_format(msk):
        d = _FORMAT_M << 3 | msk
        rem = d
        for _ in range(10):
            rem = (rem << 1) ^ ((rem >> 9) * 0x537)
        fbits = (d << 10 | rem) ^ 0x5412
        bit = lambda i: (fbits >> i) & 1 != 0  # noqa: E731
        for i in range(6):
            setf(8, i, bit(i))
        setf(8, 7, bit(6))
        setf(8, 8, bit(7))
        setf(7, 8, bit(8))
        for i in range(9, 15):
            setf(14 - i, 8, bit(i))
        for i in range(8):
            setf(size - 1 - i, 8, bit(i))
        for i in range(8, 15):
            setf(8, size - 15 + i, bit(i))
        setf(8, size - 8, True)

    draw_format(0)
    if ver >= 7:
        rem = ver
        for _ in range(12):
            rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
        vbits = ver << 12 | rem
        for i in range(18):
            b = (vbits >> i) & 1 != 0
            a, c = size - 11 + i % 3, i // 3
            setf(a, c, b)
            setf(c, a, b)

    # Codewords, zigzag
    i = 0
    total = len(final) * 8
    right = size - 1
    while right >= 1:
        if right == 6:
            right = 5
        for vert in range(size):
            for j in range(2):
                x = right - j
                upward = ((right + 1) & 2) == 0
                y = size - 1 - vert if upward else vert
                if not is_func[y][x] and i < total:
                    modules[y][x] = (final[i >> 3] >> (7 - (i & 7))) & 1 != 0
                    i += 1
        right -= 2

    masks = [
        lambda x, y: (x + y) % 2 == 0,
        lambda x, y: y % 2 == 0,
        lambda x, y: x % 3 == 0,
        lambda x, y: (x + y) % 3 == 0,
        lambda x, y: (x // 3 + y // 2) % 2 == 0,
        lambda x, y: x * y % 2 + x * y % 3 == 0,
        lambda x, y: (x * y % 2 + x * y % 3) % 2 == 0,
        lambda x, y: ((x + y) % 2 + x * y % 3) % 2 == 0,
    ]

    def apply_mask(msk):
        fn = masks[msk]
        for y in range(size):
            for x in range(size):
                if not is_func[y][x] and fn(x, y):
                    modules[y][x] = not modules[y][x]

    if mask is None:
        best, best_score = 0, None
        for msk in range(8):
            apply_mask(msk)
            draw_format(msk)
            score = _penalty(modules)
            if best_score is None or score < best_score:
                best, best_score = msk, score
            apply_mask(msk)  # undo (XOR)
        mask = best
    apply_mask(mask)
    draw_format(mask)
    return modules


def _penalty(m):
    size = len(m)
    score = 0
    lines = [row for row in m] + [[m[y][x] for y in range(size)] for x in range(size)]
    finder_a = "10111010000"
    finder_b = "00001011101"
    for line in lines:
        run, prev = 0, None
        for v in line:
            if v == prev:
                run += 1
            else:
                if run >= 5:
                    score += run - 2
                run, prev = 1, v
        if run >= 5:
            score += run - 2
        s = "0000" + "".join("1" if v else "0" for v in line) + "0000"
        score += 40 * (s.count(finder_a) + s.count(finder_b))
    for y in range(size - 1):
        for x in range(size - 1):
            v = m[y][x]
            if v == m[y][x + 1] == m[y + 1][x] == m[y + 1][x + 1]:
                score += 3
    dark = sum(v for row in m for v in row)
    total = size * size
    score += max(0, (abs(dark * 20 - total * 10) + total - 1) // total - 1) * 10
    return score


def qr_svg(m, border=4):
    size = len(m) + border * 2
    path = "".join(f"M{x + border},{y + border}h1v1h-1z"
                   for y, row in enumerate(m) for x, v in enumerate(row) if v)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}" '
            f'shape-rendering="crispEdges"><rect width="100%" height="100%" fill="#fff"/>'
            f'<path d="{path}" fill="#000"/></svg>')


def qr_terminal(m, border=2):
    """Render with half-block characters, forcing black-on-white so it scans on dark terminals."""
    size = len(m)
    get = lambda x, y: 0 <= x < size and 0 <= y < size and m[y][x]  # noqa: E731
    rows = []
    for y in range(-border, size + border, 2):
        line = ""
        for x in range(-border, size + border):
            top, bottom = get(x, y), get(x, y + 1)
            line += "█" if top and bottom else "▀" if top else "▄" if bottom else " "
        rows.append("  \x1b[30;47m" + line + "\x1b[0m")
    return "\n".join(rows)


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

def lan_ips():
    ips = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))  # UDP connect sends nothing; picks the outward interface
        ips.append(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    if sys.platform != "darwin":  # hostname lookups can stall for seconds on macOS
        try:
            for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
                ips.append(info[4][0])
        except OSError:
            pass
    seen, out = set(), []
    for ip in ips:
        if ip not in seen and not ip.startswith(("127.", "169.254.")):
            seen.add(ip)
            out.append(ip)

    def rank(ip):  # home LANs first; VPNs (often 10.x / 100.x) usually aren't what a phone can reach
        a, b = (int(x) for x in ip.split(".")[:2])
        if a == 192 and b == 168:
            return 0
        if a == 172 and 16 <= b <= 31:
            return 1
        if a == 10:
            return 2
        return 3
    return sorted(out, key=rank)


def enable_ansi():
    if os.name != "nt":
        return
    try:
        import ctypes
        k = ctypes.windll.kernel32
        h = k.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if k.GetConsoleMode(h, ctypes.byref(mode)):
            k.SetConsoleMode(h, mode.value | 0x0004)
    except Exception:
        pass


class Config:
    pass


def main(argv=None):
    p = argparse.ArgumentParser(prog="psst", description="Share a folder over HTTP with a friendly web UI.")
    p.add_argument("directory", nargs="?", default=".", help="folder to share (default: current)")
    p.add_argument("-p", "--port", type=int, help="port (default: 8000, or next free)")
    p.add_argument("-b", "--bind", default="0.0.0.0", help="address to bind (default: all interfaces)")
    p.add_argument("-r", "--read-only", action="store_true", help="disable uploads and new folders")
    p.add_argument("--password", help="require this password (HTTP basic auth, any username)")
    p.add_argument("--no-qr", action="store_true", help="don't print a QR code")
    p.add_argument("--version", action="version", version=f"psst {__version__}")
    args = p.parse_args(argv)

    root = os.path.realpath(os.path.expanduser(args.directory))  # Windows PowerShell passes ~ through unexpanded
    if not os.path.isdir(root):
        p.error(f"not a directory: {args.directory}")

    cfg = Config()
    cfg.root = root
    cfg.read_only = args.read_only
    cfg.password = args.password or os.environ.get("PSST_PASSWORD") or None
    cfg.clipboard = Clipboard()
    purge_trash(root)

    ports = [args.port] if args.port else range(8000, 8020)
    server = None
    for port in ports:
        try:
            server = Server((args.bind, port), cfg)
            break
        except OSError as e:
            last_err = e
    if server is None:
        p.error(f"could not bind to port {ports[0]}: {last_err}")
    port = server.server_address[1]

    if args.bind in ("0.0.0.0", ""):
        hosts = lan_ips()
        local = f"http://localhost:{port}/"
    else:
        hosts = [args.bind]
        local = None
    cfg.urls = [f"http://{h}:{port}/" for h in hosts]

    enable_ansi()
    tty = sys.stdout.isatty()
    bold = (lambda s: f"\x1b[1m{s}\x1b[0m") if tty else (lambda s: s)
    print(f"\n  {bold('psst')} {__version__}  serving {root}")
    mode = "read-only" if cfg.read_only else "uploads enabled"
    if cfg.password:
        mode += ", password protected"
    if Image is not None:
        mode += ", Pillow thumbnails"
    print(f"  {mode}\n")
    if local:
        print(f"  Local:    {local}")
    for i, url in enumerate(cfg.urls):
        print(f"  {'Network:' if i == 0 else '':<10}{bold(url)}")
    if not cfg.urls:
        print("  Network:  (no LAN address found)")
    if cfg.urls and tty and not args.no_qr:
        try:
            art = qr_terminal(qr_encode(cfg.urls[0].encode()))
            art.encode(sys.stdout.encoding or "ascii")
            print("\n" + art)
        except (UnicodeEncodeError, LookupError):
            pass
    print("\n  Ctrl+C to stop\n")
    sys.stdout.flush()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  stopped")
    finally:
        server.server_close()


# ---------------------------------------------------------------------------
# Web UI
# ---------------------------------------------------------------------------

APP_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>psst</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='8' fill='%232f6fde'/%3E%3Cpath d='M9 17l7-7 7 7M16 10v13' stroke='white' stroke-width='3' fill='none' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E">
<style>
:root{
  --bg:#f6f6f4;--panel:#fff;--text:#1c1c1a;--muted:#6d6d68;--line:#e5e4df;--hover:#efeee9;
  --accent:#2f6fde;--accent-text:#fff;--danger:#c23b3b;--ok:#2e8b57;
  --shadow:0 10px 40px rgba(0,0,0,.14);--radius:10px;
  --k-dir:#d99a21;--k-image:#2e9e6a;--k-video:#8a55d6;--k-audio:#d4558d;--k-text:#5878a8;--k-pdf:#d0493b;--k-archive:#9a7550;--k-file:#8a8a85;
  color-scheme:light;
}
@media (prefers-color-scheme:dark){:root{
  --bg:#141414;--panel:#1c1c1c;--text:#ebebe8;--muted:#9a9a95;--line:#2b2b2a;--hover:#242423;
  --accent:#5b8ff0;--danger:#e06363;--ok:#4fb67f;--shadow:0 10px 40px rgba(0,0,0,.5);
  color-scheme:dark;
}}
*{box-sizing:border-box}
[hidden]{display:none!important}
html,body{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;-webkit-text-size-adjust:100%}
button,input,textarea{font:inherit;color:inherit}
a{color:inherit;text-decoration:none}
svg.i{width:18px;height:18px;flex:none;stroke:currentColor;fill:none;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round}
.btn{display:inline-flex;align-items:center;gap:6px;height:34px;padding:0 12px;border:1px solid var(--line);background:var(--panel);border-radius:8px;cursor:pointer;white-space:nowrap}
.btn:hover{background:var(--hover)}
.btn.primary{background:var(--accent);border-color:var(--accent);color:var(--accent-text)}
.btn.primary:hover{filter:brightness(1.08)}
.btn.icon{padding:0;width:34px;justify-content:center}
.btn.ghost{border-color:transparent;background:transparent}
.btn.ghost:hover{background:var(--hover)}
.badge{min-width:18px;height:18px;padding:0 5px;border-radius:9px;background:var(--danger);color:#fff;font-size:11px;font-weight:600;display:inline-grid;place-items:center}
.badge:empty{display:none}

header{position:sticky;top:0;z-index:10;background:var(--bg);border-bottom:1px solid var(--line)}
.bar{max-width:1200px;margin:0 auto;padding:10px 16px;display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.brand{font-weight:700;letter-spacing:-.02em;font-size:16px;display:flex;align-items:center;gap:8px;margin-right:6px}
.brand b{width:24px;height:24px;border-radius:6px;background:var(--accent);display:grid;place-items:center;color:#fff}
.brand b svg{width:14px;height:14px;stroke-width:2.6}
.crumbs{display:flex;align-items:center;gap:2px;min-width:0;flex:1;overflow-x:auto;scrollbar-width:none}
.crumbs::-webkit-scrollbar{display:none}
.crumbs a{padding:4px 6px;border-radius:6px;white-space:nowrap;color:var(--muted)}
.crumbs a:last-child{color:var(--text);font-weight:600}
.crumbs a:hover{background:var(--hover)}
.crumbs span{color:var(--muted);opacity:.6}
.actions{display:flex;gap:6px;align-items:center}
.filter{height:34px;width:180px;padding:0 10px;border:1px solid var(--line);border-radius:8px;background:var(--panel);outline:none}
.filter:focus{border-color:var(--accent)}

main{max-width:1200px;margin:0 auto;padding:12px 16px 120px}
.rows{background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);overflow:hidden}
.row{display:grid;grid-template-columns:28px 1fr 90px 130px 40px;align-items:center;gap:10px;padding:0 10px 0 14px;height:44px;border-top:1px solid var(--line);cursor:pointer}
.row:first-child{border-top:0}
.row:hover{background:var(--hover)}
.row.head{height:34px;cursor:default;color:var(--muted);font-size:12px;background:transparent}
.row.head:hover{background:transparent}
.row.head button{background:none;border:0;padding:0;cursor:pointer;color:inherit;text-align:left;font-size:12px}
.row.head button.on{color:var(--text);font-weight:600}
.row .name{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.row .size,.row .date{color:var(--muted);font-variant-numeric:tabular-nums;font-size:13px}
.row .size{text-align:right}
.row .sub{display:none}
.row .dl{opacity:0;justify-self:end}
.row:hover .dl,.row .dl:focus{opacity:1}
.k-dir{color:var(--k-dir)}.k-image{color:var(--k-image)}.k-video{color:var(--k-video)}.k-audio{color:var(--k-audio)}
.k-text{color:var(--k-text)}.k-pdf{color:var(--k-pdf)}.k-archive{color:var(--k-archive)}.k-file{color:var(--k-file)}
.flash{animation:flash 2.4s ease-out}
@keyframes flash{0%,30%{background:color-mix(in srgb,var(--accent) 18%,transparent)}100%{background:transparent}}

.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:10px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);overflow:hidden;cursor:pointer;display:flex;flex-direction:column}
.card:hover{border-color:var(--muted)}
.card .thumb{aspect-ratio:1;display:grid;place-items:center;background:var(--hover);overflow:hidden}
.card .thumb img{width:100%;height:100%;object-fit:cover;display:block}
.card .thumb svg.i{width:40px;height:40px;stroke-width:1.4}
.card .cap{padding:7px 9px;font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.card .cap small{display:block;color:var(--muted)}

.empty{padding:70px 20px;text-align:center;color:var(--muted)}
.empty svg.i{width:40px;height:40px;stroke-width:1.3;margin-bottom:10px}

.drop{position:fixed;inset:0;z-index:50;display:none;place-items:center;background:color-mix(in srgb,var(--accent) 14%,transparent);backdrop-filter:blur(2px)}
.drop.on{display:grid}
.drop div{border:2px dashed var(--accent);border-radius:16px;padding:40px 60px;background:var(--panel);font-size:16px;font-weight:600;box-shadow:var(--shadow);pointer-events:none}

.uploads{position:fixed;right:16px;bottom:16px;z-index:40;width:360px;max-width:calc(100vw - 32px);background:var(--panel);border:1px solid var(--line);border-radius:12px;box-shadow:var(--shadow);overflow:hidden}
.uploads header{position:static;display:flex;align-items:center;gap:8px;padding:10px 10px 10px 14px;background:transparent;border-bottom:1px solid var(--line)}
.uploads header .t{flex:1;font-weight:600;font-size:13px}
.uploads header .t small{display:block;font-weight:400;color:var(--muted)}
.uploads .list{max-height:260px;overflow:auto}
.uploads.min .list{display:none}
.up{padding:8px 14px;border-top:1px solid var(--line);font-size:13px}
.up:first-child{border-top:0}
.up .n{display:flex;justify-content:space-between;gap:8px}
.up .n span:first-child{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.up .n span:last-child{color:var(--muted);flex:none;font-variant-numeric:tabular-nums}
.up .bar2{height:3px;background:var(--hover);border-radius:2px;margin-top:6px;overflow:hidden}
.up .bar2 i{display:block;height:100%;width:0;background:var(--accent);transition:width .15s}
.up.done .bar2 i{background:var(--ok)}
.up.err .bar2 i{background:var(--danger);width:100%!important}
.up.err .n span:last-child{color:var(--danger)}

.clip{position:fixed;top:0;right:0;bottom:0;z-index:30;width:380px;max-width:100vw;background:var(--panel);border-left:1px solid var(--line);box-shadow:var(--shadow);display:flex;flex-direction:column;transform:translateX(100%);transition:transform .2s ease;visibility:hidden}
.clip.on{transform:none;visibility:visible}
.clip>header{position:static;display:flex;align-items:center;gap:8px;padding:12px 12px 12px 16px;background:transparent}
.clip>header h2{flex:1;margin:0;font-size:15px}
.clip .compose{padding:0 16px 12px;border-bottom:1px solid var(--line)}
.clip textarea{width:100%;min-height:90px;max-height:40vh;resize:vertical;padding:10px;border:1px solid var(--line);border-radius:8px;background:var(--bg);outline:none}
.clip textarea:focus{border-color:var(--accent)}
.clip .compose div{display:flex;justify-content:space-between;align-items:center;margin-top:8px;color:var(--muted);font-size:12px}
.clip .items{flex:1;overflow:auto;padding:8px 0}
.ci{padding:10px 16px;border-bottom:1px solid var(--line)}
.ci pre{margin:0;white-space:pre-wrap;word-break:break-word;font:13px/1.45 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;max-height:9em;overflow:hidden;position:relative}
.ci.open pre{max-height:none}
.ci .meta{display:flex;align-items:center;gap:4px;margin-top:6px;color:var(--muted);font-size:12px}
.ci .meta span{flex:1}
.ci .meta .btn{height:28px;padding:0 9px;font-size:12px}
.clip .none{padding:30px 16px;color:var(--muted);text-align:center}

.viewer{position:fixed;inset:0;z-index:60;background:rgba(10,10,10,.97);color:#eee;display:none;flex-direction:column}
.viewer.on{display:flex}
.viewer .vt{display:flex;align-items:center;gap:6px;padding:10px 12px;padding-top:max(10px,env(safe-area-inset-top))}
.viewer .vt .t{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-weight:600}
.viewer .vt .t small{color:#999;font-weight:400;margin-left:8px}
.viewer .btn{background:rgba(255,255,255,.08);border-color:transparent;color:#eee}
.viewer .btn:hover{background:rgba(255,255,255,.16)}
.viewer .vb{flex:1;min-height:0;display:grid;place-items:center;position:relative;padding:0 12px 12px}
.viewer .vb>img,.viewer .vb>video{max-width:100%;max-height:100%;object-fit:contain;display:block}
.viewer .vb>iframe{width:100%;height:100%;border:0;background:#fff;border-radius:6px}
.viewer .vb>audio{width:min(600px,100%)}
.viewer .txt{width:min(1000px,100%);height:100%;overflow:auto;background:#1b1b1b;border:1px solid #2c2c2c;border-radius:8px;padding:14px 16px;margin:0;font:13px/1.55 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;white-space:pre;tab-size:4;color:#ddd}
.viewer .txt.wrap{white-space:pre-wrap;word-break:break-word}
.viewer .note{color:#aaa;text-align:center}
.viewer .note svg.i{width:48px;height:48px;stroke-width:1.2;margin-bottom:8px}
.viewer .nav{position:absolute;top:50%;transform:translateY(-50%);width:44px;height:64px;border:0;border-radius:8px;background:rgba(255,255,255,.06);color:#eee;cursor:pointer;display:grid;place-items:center}
.viewer .nav:hover{background:rgba(255,255,255,.16)}
.viewer .nav.prev{left:12px}.viewer .nav.next{right:12px}

.modal{position:fixed;inset:0;z-index:70;background:rgba(0,0,0,.45);display:none;place-items:center;padding:16px}
.modal.on{display:grid}
.modal .box{background:var(--panel);border-radius:14px;box-shadow:var(--shadow);padding:20px;width:min(420px,100%);max-height:90vh;overflow:auto}
.modal h2{margin:0 0 4px;font-size:16px}
.modal p{margin:0 0 14px;color:var(--muted)}
.qr{display:flex;flex-direction:column;align-items:center;gap:8px;margin-bottom:16px}
.qr img{width:220px;height:220px;border-radius:8px;background:#fff}
.qr code{font-size:13px;user-select:all;word-break:break-all;text-align:center}

.toast{position:fixed;left:50%;bottom:20px;transform:translateX(-50%) translateY(20px);z-index:80;background:var(--text);color:var(--bg);padding:9px 16px;border-radius:8px;opacity:0;pointer-events:none;transition:all .2s;font-size:13px;max-width:calc(100vw - 32px)}
.toast.on{opacity:1;transform:translateX(-50%)}

.lbl{display:inline}
@media (max-width:820px){
  .lbl{display:none}
  .btn.hasl{padding:0;width:34px;justify-content:center}
  .filter{width:120px}
}
@media (max-width:640px){
  .bar{padding:8px 12px}
  .crumbs{order:3;flex-basis:100%}
  .actions{margin-left:auto}
  .filter{display:none}
  main{padding:8px 8px 120px}
  .row{grid-template-columns:28px 1fr 40px;height:52px}
  .row .size,.row .date{display:none}
  .row .sub{display:block;color:var(--muted);font-size:12px}
  .row .dl{opacity:1}
  .row.head{display:none}
  .grid{grid-template-columns:repeat(auto-fill,minmax(110px,1fr));gap:6px}
  .viewer .nav{display:none}
  .clip{width:100vw}
}
</style>
</head>
<body>
<header>
  <div class="bar">
    <a class="brand" href="/" data-nav><b><svg class="i" viewBox="0 0 24 24"><path d="M6 13l6-6 6 6M12 7v12"/></svg></b>psst</a>
    <nav class="crumbs" id="crumbs"></nav>
    <div class="actions">
      <input class="filter" id="filter" type="search" placeholder="Filter  /" autocomplete="off">
      <button class="btn icon ghost" id="viewBtn" title="Toggle grid / list"></button>
      <button class="btn ghost hasl rw" id="mkdirBtn" title="New folder"><svg class="i" viewBox="0 0 24 24"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/><path d="M12 10v6M9 13h6"/></svg></button>
      <a class="btn ghost hasl" id="zipBtn" title="Download this folder as .zip"><svg class="i" viewBox="0 0 24 24"><path d="M12 4v11M7 10l5 5 5-5M5 20h14"/></svg><span class="lbl">Zip</span></a>
      <button class="btn ghost hasl" id="qrBtn" title="Open on another device"><svg class="i" viewBox="0 0 24 24"><rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><path d="M14 14h3v3h-3zM20 14v.01M14 20h.01M17 20h4v-3"/></svg><span class="lbl">Connect</span></button>
      <button class="btn hasl" id="clipBtn" title="Shared clipboard"><svg class="i" viewBox="0 0 24 24"><rect x="6" y="4" width="12" height="17" rx="2"/><path d="M9 4.5V3h6v1.5M9 10h6M9 14h6"/></svg><span class="lbl">Clipboard</span><span class="badge" id="clipBadge"></span></button>
      <button class="btn primary hasl rw" id="uploadBtn" title="Upload files"><svg class="i" viewBox="0 0 24 24"><path d="M12 19V8M7 13l5-5 5 5M5 4h14"/></svg><span class="lbl">Upload</span></button>
      <button class="btn icon rw" id="folderBtn" title="Upload a folder"><svg class="i" viewBox="0 0 24 24"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/><path d="M12 16v-5M9.5 13.5 12 11l2.5 2.5"/></svg></button>
      <input type="file" id="fileInput" multiple hidden>
      <input type="file" id="folderInput" webkitdirectory multiple hidden>
    </div>
  </div>
</header>

<main id="main"></main>

<aside class="clip" id="clip">
  <header><h2>Shared clipboard</h2><button class="btn ghost" id="clipClear" title="Clear all">Clear</button><button class="btn icon ghost" id="clipClose" title="Close"><svg class="i" viewBox="0 0 24 24"><path d="M6 6l12 12M18 6 6 18"/></svg></button></header>
  <div class="compose">
    <textarea id="clipText" placeholder="Paste or type text to share with your other devices"></textarea>
    <div><span>Ctrl+Enter to share</span><button class="btn primary" id="clipSend">Share</button></div>
  </div>
  <div class="items" id="clipItems"></div>
</aside>

<div class="uploads" id="uploads" hidden>
  <header><div class="t" id="upTitle"></div>
    <button class="btn icon ghost" id="upMin" title="Collapse"><svg class="i" viewBox="0 0 24 24"><path d="M6 9l6 6 6-6"/></svg></button>
    <button class="btn icon ghost" id="upClose" title="Dismiss"><svg class="i" viewBox="0 0 24 24"><path d="M6 6l12 12M18 6 6 18"/></svg></button></header>
  <div class="list" id="upList"></div>
</div>

<div class="drop" id="drop"><div id="dropText">Drop to upload</div></div>

<div class="viewer" id="viewer">
  <div class="vt">
    <div class="t" id="vTitle"></div>
    <button class="btn" id="vWrap" title="Toggle line wrap" hidden>Wrap</button>
    <button class="btn" id="vCopy" title="Copy text" hidden>Copy</button>
    <a class="btn icon" id="vOpen" title="Open in new tab" target="_blank" rel="noopener"><svg class="i" viewBox="0 0 24 24"><path d="M14 4h6v6M20 4l-9 9M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5"/></svg></a>
    <a class="btn icon" id="vDl" title="Download"><svg class="i" viewBox="0 0 24 24"><path d="M12 4v11M7 10l5 5 5-5M5 20h14"/></svg></a>
    <button class="btn icon" id="vClose" title="Close (Esc)"><svg class="i" viewBox="0 0 24 24"><path d="M6 6l12 12M18 6 6 18"/></svg></button>
  </div>
  <div class="vb" id="vBody"></div>
</div>

<div class="modal" id="qrModal"><div class="box" id="qrBox"></div></div>
<div class="toast" id="toast"></div>

<script>
"use strict";
const $ = s => document.querySelector(s);
const ICONS = {
  dir:'<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>',
  file:'<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/>',
  image:'<rect x="3" y="4" width="18" height="16" rx="2"/><circle cx="9" cy="10" r="1.6"/><path d="M21 16l-5-5-9 9"/>',
  video:'<rect x="3" y="5" width="13" height="14" rx="2"/><path d="M16 10l5-3v10l-5-3"/>',
  audio:'<path d="M9 18V6l11-2v12"/><circle cx="6.5" cy="18" r="2.5"/><circle cx="17.5" cy="16" r="2.5"/>',
  text:'<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5M9 13h6M9 17h4"/>',
  pdf:'<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5M9 15h1.5a1.5 1.5 0 0 0 0-3H9v5"/>',
  archive:'<rect x="3" y="4" width="18" height="5" rx="1"/><path d="M5 9v10a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1V9M10 13h4"/>',
  download:'<path d="M12 4v11M7 10l5 5 5-5M5 20h14"/>',
  grid:'<rect x="4" y="4" width="7" height="7" rx="1"/><rect x="13" y="4" width="7" height="7" rx="1"/><rect x="4" y="13" width="7" height="7" rx="1"/><rect x="13" y="13" width="7" height="7" rx="1"/>',
  list:'<path d="M8 6h12M8 12h12M8 18h12M4 6h.01M4 12h.01M4 18h.01"/>',
  left:'<path d="M15 6l-6 6 6 6"/>', right:'<path d="M9 6l6 6-6 6"/>',
};
const icon = (n, cls="") => `<svg class="i ${cls}" viewBox="0 0 24 24">${ICONS[n] || ICONS.file}</svg>`;
const esc = s => String(s).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const enc = s => encodeURIComponent(s);
function fmtSize(n){ if(n<1024) return n+" B"; const u=["KB","MB","GB","TB"]; let i=-1; do{n/=1024;i++}while(n>=1024&&i<u.length-1); return (n<10?n.toFixed(1):Math.round(n))+" "+u[i]; }
function fmtTime(t){
  const d = new Date(t*1000), s = (Date.now()-d)/1000;
  if (s < 60) return "just now";
  if (s < 3600) return Math.floor(s/60)+" min ago";
  if (s < 86400) return Math.floor(s/3600)+" h ago";
  if (s < 86400*6) return d.toLocaleDateString(undefined,{weekday:"short"})+" "+d.toLocaleTimeString(undefined,{hour:"2-digit",minute:"2-digit"});
  return d.toLocaleDateString(undefined,{year:"numeric",month:"short",day:"numeric"});
}
let toastTimer;
function toast(msg){ const t=$("#toast"); t.textContent=msg; t.classList.add("on"); clearTimeout(toastTimer); toastTimer=setTimeout(()=>t.classList.remove("on"),2200); }
function copyText(text){
  if (navigator.clipboard && window.isSecureContext) return navigator.clipboard.writeText(text).then(()=>true,()=>fallbackCopy(text));
  return Promise.resolve(fallbackCopy(text));
}
function fallbackCopy(text){
  const ta=document.createElement("textarea"); ta.value=text; ta.setAttribute("readonly",""); ta.style.cssText="position:fixed;top:0;left:0;opacity:0";
  document.body.appendChild(ta); ta.select(); ta.setSelectionRange(0, text.length);
  let ok=false; try{ ok=document.execCommand("copy"); }catch(e){}
  ta.remove(); return ok;
}

// ---------------------------------------------------------------- state
const state = { path: location.pathname, entries: [], readOnly: false, sort: "name", desc: false, filter: "", flash: new Set() };
let viewPref = localStorage.getItem("psst.view") || "auto";

function visibleEntries(){
  const f = state.filter.toLowerCase();
  const list = state.entries.filter(e => !f || e.name.toLowerCase().includes(f));
  const k = state.sort, m = state.desc ? -1 : 1;
  list.sort((a,b) => (b.dir - a.dir) || m * (k==="size" ? a.size-b.size : k==="mtime" ? a.mtime-b.mtime : a.name.localeCompare(b.name, undefined, {numeric:true, sensitivity:"base"})));
  return list;
}
function effectiveView(){
  if (viewPref !== "auto") return viewPref;
  const files = state.entries.filter(e => !e.dir);
  const imgs = files.filter(e => e.kind === "image").length;
  return imgs >= 4 && imgs >= files.length * 0.6 ? "grid" : "list";
}
const fileUrl = e => state.path + enc(e.name) + (e.dir ? "/" : "");

// ---------------------------------------------------------------- listing
async function load(path, push){
  if (push) history.pushState(null, "", path);
  state.path = path;
  state.filter = ""; $("#filter").value = "";
  renderCrumbs();
  try {
    const r = await fetch(path + "?json", {cache:"no-store"});
    if (!r.ok) throw new Error(r.status === 404 ? "Folder not found" : "Error " + r.status);
    const data = await r.json();
    state.entries = data.entries; state.readOnly = data.readOnly;
    document.body.classList.toggle("ro", data.readOnly);
    document.querySelectorAll(".rw").forEach(el => el.hidden = data.readOnly);
    render();
  } catch (err) {
    state.entries = [];
    $("#main").innerHTML = `<div class="empty">${icon("dir")}<div>${esc(err.message)}</div><p><a class="btn" href="/" data-nav>Go to top</a></p></div>`;
  }
}
async function refresh(){
  const r = await fetch(state.path + "?json", {cache:"no-store"}).catch(()=>null);
  if (r && r.ok){ state.entries = (await r.json()).entries; render(); }
}
function renderCrumbs(){
  const parts = state.path.split("/").filter(Boolean);
  let acc = "/", html = `<a href="/" data-nav>Home</a>`;
  for (const p of parts){ acc += p + "/"; html += `<span>/</span><a href="${esc(acc)}" data-nav>${esc(decodeURIComponent(p))}</a>`; }
  $("#crumbs").innerHTML = html;
  const last = $("#crumbs").lastElementChild; if (last) last.scrollIntoView({inline:"end", block:"nearest"});
  document.title = (parts.length ? decodeURIComponent(parts[parts.length-1]) : "Home") + " · psst";
  $("#zipBtn").href = state.path + "?zip";
}
function render(){
  const list = visibleEntries(), view = effectiveView();
  $("#viewBtn").innerHTML = icon(view === "grid" ? "list" : "grid");
  const main = $("#main");
  if (!list.length){
    main.innerHTML = `<div class="empty">${icon(state.filter ? "file" : "dir")}<div>${state.filter ? "No matches" : "This folder is empty"}</div>${!state.filter && !state.readOnly ? "<div>Drop files anywhere, or paste an image, to upload</div>" : ""}</div>`;
    return;
  }
  const arrow = k => state.sort === k ? (state.desc ? " ↓" : " ↑") : "";
  let html;
  if (view === "grid"){
    html = `<div class="grid">` + list.map((e,i) => `<div class="card${state.flash.has(e.name)?" flash":""}" data-i="${i}">
      <div class="thumb">${e.kind==="image" ? `<img loading="lazy" decoding="async" src="${esc(fileUrl(e))}" alt="">` : icon(e.kind, "k-"+e.kind)}</div>
      <div class="cap">${esc(e.name)}<small>${e.dir ? "Folder" : fmtSize(e.size)}</small></div></div>`).join("") + `</div>`;
  } else {
    html = `<div class="rows"><div class="row head"><span></span>
      <button data-sort="name" class="${state.sort==="name"?"on":""}">Name${arrow("name")}</button>
      <button data-sort="size" class="${state.sort==="size"?"on":""}" style="text-align:right">Size${arrow("size")}</button>
      <button data-sort="mtime" class="${state.sort==="mtime"?"on":""}">Modified${arrow("mtime")}</button><span></span></div>` +
      list.map((e,i) => `<div class="row${state.flash.has(e.name)?" flash":""}" data-i="${i}">
        ${icon(e.kind, "k-"+e.kind)}
        <div class="name" title="${esc(e.name)}">${esc(e.name)}<div class="sub">${e.dir ? "Folder" : fmtSize(e.size)} · ${fmtTime(e.mtime)}</div></div>
        <span class="size">${e.dir ? "" : fmtSize(e.size)}</span>
        <span class="date" title="${esc(new Date(e.mtime*1000).toLocaleString())}">${fmtTime(e.mtime)}</span>
        <a class="btn icon ghost dl" href="${esc(fileUrl(e))}${e.dir ? "?zip" : "?dl"}" title="${e.dir ? "Download as .zip" : "Download"}" download>${icon("download")}</a>
      </div>`).join("") + `</div>`;
  }
  main.innerHTML = html;
  state.flash.clear();
  main.onclick = ev => {
    const s = ev.target.closest("[data-sort]");
    if (s){ const k=s.dataset.sort; state.desc = state.sort===k ? !state.desc : k!=="name"; state.sort=k; render(); return; }
    if (ev.target.closest(".dl")) return;
    const el = ev.target.closest("[data-i]"); if (!el) return;
    const e = list[+el.dataset.i];
    if (e.dir) load(fileUrl(e), true);
    else openViewer(list.filter(x => PREVIEW.has(x.kind)), e);
  };
}
$("#viewBtn").onclick = () => { viewPref = effectiveView() === "grid" ? "list" : "grid"; localStorage.setItem("psst.view", viewPref); render(); };
$("#filter").oninput = e => { state.filter = e.target.value; render(); };
document.addEventListener("click", e => {
  const a = e.target.closest("a[data-nav]");
  if (a && !e.ctrlKey && !e.metaKey && !e.shiftKey){ e.preventDefault(); load(a.getAttribute("href"), true); }
});
window.addEventListener("popstate", () => { closeViewer(true); load(location.pathname, false); });
document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible") { refresh(); pollClip(); } });

// ---------------------------------------------------------------- viewer
let vList = [], vIdx = 0, vText = "";
const PREVIEW = new Set(["image", "video", "audio", "pdf", "text"]);
function openViewer(list, entry){
  if (!PREVIEW.has(entry.kind)){ location.href = fileUrl(entry) + "?dl"; return; }
  vList = list; vIdx = list.indexOf(entry);
  $("#viewer").classList.add("on"); document.body.style.overflow = "hidden";
  showItem();
}
function closeViewer(){
  $("#viewer").classList.remove("on"); document.body.style.overflow = "";
  $("#vBody").innerHTML = "";
}
async function showItem(){
  const e = vList[vIdx], url = fileUrl(e), body = $("#vBody");
  $("#vTitle").innerHTML = `${esc(e.name)}<small>${fmtSize(e.size)}${vList.length>1 ? " · "+(vIdx+1)+" / "+vList.length : ""}</small>`;
  $("#vOpen").href = url; $("#vDl").href = url + "?dl";
  $("#vCopy").hidden = $("#vWrap").hidden = e.kind !== "text";
  const nav = vList.length > 1 ? `<button class="nav prev" data-d="-1">${icon("left")}</button><button class="nav next" data-d="1">${icon("right")}</button>` : "";
  if (e.kind === "image"){
    body.innerHTML = `<img src="${esc(url)}" alt="">` + nav;
    for (const d of [-1, 1]){ const n = vList[(vIdx+d+vList.length)%vList.length]; if (n.kind==="image") new Image().src = fileUrl(n); }
  } else if (e.kind === "video"){
    body.innerHTML = `<video src="${esc(url)}" controls autoplay playsinline></video>` + nav;
  } else if (e.kind === "audio"){
    body.innerHTML = `<div class="note">${icon("audio")}<div>${esc(e.name)}</div><br><audio src="${esc(url)}" controls autoplay></audio></div>` + nav;
  } else if (e.kind === "pdf"){
    body.innerHTML = `<iframe src="${esc(url)}"></iframe>` + nav;
  } else if (e.kind === "text"){
    body.innerHTML = `<div class="note">Loading…</div>`;
    const LIMIT = 2*1024*1024, want = vIdx;
    try {
      const r = await fetch(url, {headers: e.size > LIMIT ? {Range: "bytes=0-"+(LIMIT-1)} : {}, cache:"no-store"});
      const buf = await r.arrayBuffer();
      if (want !== vIdx || !$("#viewer").classList.contains("on")) return;
      vText = new TextDecoder("utf-8").decode(buf);
      const wrap = localStorage.getItem("psst.wrap") === "1";
      body.innerHTML = (e.size > LIMIT ? `<div class="note" style="margin-bottom:8px">Showing the first 2 MB of ${fmtSize(e.size)}</div>` : "") +
        `<pre class="txt${wrap?" wrap":""}"></pre>` + nav;
      body.querySelector("pre").textContent = vText;
      if (e.size > LIMIT) body.style.gridTemplateRows = "auto 1fr"; else body.style.gridTemplateRows = "";
    } catch (err) { body.innerHTML = `<div class="note">Could not load file</div>`; }
  }
}
function step(d){ if (vList.length < 2) return; vIdx = (vIdx + d + vList.length) % vList.length; $("#vBody").style.gridTemplateRows=""; showItem(); }
$("#vBody").addEventListener("click", e => { const b = e.target.closest(".nav"); if (b) step(+b.dataset.d); else if (e.target === $("#vBody")) closeViewer(); });
$("#vClose").onclick = closeViewer;
$("#vCopy").onclick = () => copyText(vText).then(ok => toast(ok ? "Copied" : "Copy failed"));
$("#vWrap").onclick = () => { const p = $("#vBody pre"); if (!p) return; p.classList.toggle("wrap"); localStorage.setItem("psst.wrap", p.classList.contains("wrap") ? "1" : "0"); };
let touchX = null;
$("#viewer").addEventListener("touchstart", e => { touchX = e.touches.length === 1 ? e.touches[0].clientX : null; }, {passive:true});
$("#viewer").addEventListener("touchend", e => {
  if (touchX === null || vList[vIdx]?.kind === "text") return;
  const dx = e.changedTouches[0].clientX - touchX; if (Math.abs(dx) > 60) step(dx < 0 ? 1 : -1); touchX = null;
});

// ---------------------------------------------------------------- uploads
const uploads = { queue: [], active: 0, done: 0, failed: 0, total: 0, bytes: 0, sent: 0, started: 0, items: [] };
const MAX_PARALLEL = 3;
function enqueue(files){
  if (state.readOnly){ toast("Server is read-only"); return; }
  if (!files.length) return;
  if (!uploads.active && !uploads.queue.length){ Object.assign(uploads, {done:0, failed:0, total:0, bytes:0, sent:0, started:performance.now()}); $("#upList").innerHTML = ""; }
  const dir = state.path;
  for (const {file, rel} of files){
    const el = document.createElement("div"); el.className = "up";
    el.innerHTML = `<div class="n"><span></span><span>waiting</span></div><div class="bar2"><i></i></div>`;
    el.querySelector("span").textContent = rel;
    $("#upList").appendChild(el);
    uploads.queue.push({file, rel, dir, el, loaded: 0});
    uploads.total++; uploads.bytes += file.size;
  }
  $("#uploads").hidden = false; $("#uploads").classList.remove("min");
  pump(); updateTray();
}
function pump(){
  while (uploads.active < MAX_PARALLEL && uploads.queue.length){
    const u = uploads.queue.shift(); uploads.active++;
    const xhr = new XMLHttpRequest();
    xhr.open("PUT", u.dir + u.rel.split("/").map(enc).join("/"));
    const status = u.el.querySelector(".n span:last-child"), bar = u.el.querySelector("i");
    xhr.upload.onprogress = ev => { uploads.sent += ev.loaded - u.loaded; u.loaded = ev.loaded; bar.style.width = (ev.loaded / (ev.total||1) * 100) + "%"; status.textContent = Math.floor(ev.loaded/(ev.total||1)*100) + "%"; updateTray(); };
    xhr.onload = () => {
      if (xhr.status === 201){
        const res = JSON.parse(xhr.responseText);
        u.el.classList.add("done"); bar.style.width = "100%"; status.textContent = fmtSize(u.file.size);
        uploads.done++;
        if (u.dir === state.path) state.flash.add(res.path.split("/")[0]);
      } else fail(xhr.status === 403 ? "read-only" : "error " + xhr.status);
      finish();
    };
    xhr.onerror = () => { fail("failed"); finish(); };
    function fail(msg){ u.el.classList.add("err"); status.textContent = msg; uploads.failed++; }
    function finish(){ uploads.sent += u.file.size - u.loaded; u.loaded = u.file.size; uploads.active--; pump(); updateTray(); scheduleRefresh(); }
    status.textContent = "0%";
    xhr.send(u.file);
  }
}
let refreshTimer;
function scheduleRefresh(){ clearTimeout(refreshTimer); refreshTimer = setTimeout(refresh, 250); }
function updateTray(){
  const busy = uploads.active || uploads.queue.length, n = uploads.done + uploads.failed;
  const secs = (performance.now() - uploads.started) / 1000, rate = uploads.sent / Math.max(secs, .1);
  let t = busy ? `Uploading ${Math.min(n+1, uploads.total)} of ${uploads.total}` : uploads.failed ? `${uploads.done} uploaded, ${uploads.failed} failed` : `${uploads.done} upload${uploads.done===1?"":"s"} complete`;
  const sub = busy ? `${fmtSize(uploads.sent)} of ${fmtSize(uploads.bytes)} · ${fmtSize(rate)}/s` : `${fmtSize(uploads.bytes)} in ${secs < 60 ? secs.toFixed(1)+" s" : Math.round(secs/60)+" min"}`;
  $("#upTitle").innerHTML = `${esc(t)}<small>${esc(sub)}</small>`;
}
$("#upMin").onclick = () => $("#uploads").classList.toggle("min");
$("#upClose").onclick = () => { if (uploads.active || uploads.queue.length) $("#uploads").classList.add("min"); else $("#uploads").hidden = true; };
window.addEventListener("beforeunload", e => { if (uploads.active || uploads.queue.length){ e.preventDefault(); e.returnValue = ""; } });

$("#uploadBtn").onclick = () => $("#fileInput").click();
$("#folderBtn").onclick = () => $("#folderInput").click();
if (!("webkitdirectory" in document.createElement("input")) || /iPhone|iPad|Android/i.test(navigator.userAgent)) $("#folderBtn").remove();
$("#fileInput").onchange = e => { enqueue([...e.target.files].map(f => ({file:f, rel:f.name}))); e.target.value = ""; };
$("#folderInput").onchange = e => { enqueue([...e.target.files].map(f => ({file:f, rel:f.webkitRelativePath || f.name}))); e.target.value = ""; };

// drag & drop, including folders
let dragDepth = 0;
const hasFiles = e => e.dataTransfer && [...e.dataTransfer.types].includes("Files");
document.addEventListener("dragenter", e => { if (!hasFiles(e) || state.readOnly) return; e.preventDefault(); dragDepth++; $("#dropText").textContent = "Drop to upload to " + decodeURIComponent(state.path); $("#drop").classList.add("on"); });
document.addEventListener("dragover", e => { if (hasFiles(e)) e.preventDefault(); });
document.addEventListener("dragleave", e => { if (!hasFiles(e)) return; if (--dragDepth <= 0){ dragDepth = 0; $("#drop").classList.remove("on"); } });
document.addEventListener("drop", async e => {
  if (!hasFiles(e)) return;
  e.preventDefault(); dragDepth = 0; $("#drop").classList.remove("on");
  if (state.readOnly) return;
  const items = [...e.dataTransfer.items].filter(i => i.kind === "file");
  const entries = items.map(i => i.webkitGetAsEntry && i.webkitGetAsEntry());
  if (entries.every(Boolean)){
    const out = [];
    await Promise.all(entries.map(en => walk(en, "", out)));
    enqueue(out);
  } else enqueue([...e.dataTransfer.files].map(f => ({file:f, rel:f.name})));
});
function walk(entry, prefix, out){
  return new Promise(resolve => {
    if (entry.isFile) entry.file(f => { out.push({file:f, rel:prefix + f.name}); resolve(); }, () => resolve());
    else if (entry.isDirectory){
      const reader = entry.createReader(), all = [];
      const read = () => reader.readEntries(async batch => {
        if (batch.length){ all.push(...batch); read(); }
        else { await Promise.all(all.map(c => walk(c, prefix + entry.name + "/", out))); resolve(); }
      }, () => resolve());
      read();
    } else resolve();
  });
}

// paste: images/files upload, text goes to the clipboard composer
document.addEventListener("paste", e => {
  const tag = (e.target.tagName || "").toLowerCase();
  if (tag === "textarea" || tag === "input") return;
  const files = [...(e.clipboardData?.files || [])];
  if (files.length && !state.readOnly){
    e.preventDefault();
    const stamp = new Date().toISOString().slice(0,19).replace(/[T:]/g, "-");
    enqueue(files.map((f, i) => {
      const generic = !f.name || /^image\.\w+$/.test(f.name);
      const ext = (f.type.split("/")[1] || "bin").replace("jpeg", "jpg").replace(/\+.*/, "");
      return {file:f, rel: generic ? `pasted-${stamp}${files.length>1 ? "-"+(i+1) : ""}.${ext}` : f.name};
    }));
    return;
  }
  const text = e.clipboardData?.getData("text");
  if (text){ e.preventDefault(); openClip(); $("#clipText").value = text; $("#clipText").focus(); }
});

// ---------------------------------------------------------------- new folder
$("#mkdirBtn").onclick = async () => {
  const name = prompt("New folder name");
  if (!name || !name.trim()) return;
  const r = await fetch("/__psst/mkdir", {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify({path: state.path, name})});
  const res = await r.json().catch(() => ({}));
  if (r.ok){ state.flash.add(res.name); refresh(); } else toast(res.error || "Could not create folder");
};

// ---------------------------------------------------------------- clipboard
let clipV = null, clipItems = [], clipSeen = +(localStorage.getItem("psst.clipSeen") || 0), clipFirst = true;
function openClip(){ $("#clip").classList.add("on"); markSeen(); }
function closeClip(){ $("#clip").classList.remove("on"); }
function markSeen(){ if (clipItems[0]) { clipSeen = clipItems[0].id; localStorage.setItem("psst.clipSeen", clipSeen); } $("#clipBadge").textContent = ""; }
$("#clipBtn").onclick = () => $("#clip").classList.contains("on") ? closeClip() : openClip();
$("#clipClose").onclick = closeClip;
async function pollClip(){
  try {
    const r = await fetch("/__psst/clip" + (clipV !== null ? "?v=" + clipV : ""), {cache:"no-store"});
    const d = await r.json();
    if (d.items){
      clipV = d.v; clipItems = d.items; renderClip();
      if (clipItems.length && clipItems[0].id < clipSeen) clipSeen = 0;   // server restarted
      const unseen = clipItems.filter(i => i.id > clipSeen).length;
      if ($("#clip").classList.contains("on")) markSeen();
      else { $("#clipBadge").textContent = unseen || ""; if (unseen && !clipFirst) toast("New clipboard item"); }
      clipFirst = false;
    }
  } catch (e) {}
}
function renderClip(){
  const box = $("#clipItems");
  if (!clipItems.length){ box.innerHTML = `<div class="none">Nothing shared yet.<br>Text you share here shows up on every device.</div>`; return; }
  box.innerHTML = clipItems.map(it => {
    const url = /^https?:\/\/\S+$/.test(it.text.trim()) ? it.text.trim() : null;
    return `<div class="ci" data-id="${it.id}"><pre></pre><div class="meta"><span>${esc(it.from)} · ${fmtTime(it.time)}</span>
      ${url ? `<a class="btn" href="${esc(url)}" target="_blank" rel="noopener">Open</a>` : ""}
      ${it.text.split("\n").length > 6 || it.text.length > 400 ? `<button class="btn ghost" data-act="more">More</button>` : ""}
      <button class="btn" data-act="copy">Copy</button><button class="btn ghost" data-act="del" title="Delete">✕</button></div></div>`;
  }).join("");
  box.querySelectorAll(".ci").forEach((el, i) => el.querySelector("pre").textContent = clipItems[i].text);
}
$("#clipItems").onclick = async e => {
  const b = e.target.closest("[data-act]"); if (!b) return;
  const el = b.closest(".ci"), id = +el.dataset.id, it = clipItems.find(x => x.id === id);
  if (b.dataset.act === "copy") toast(await copyText(it.text) ? "Copied" : "Copy failed: select the text manually");
  if (b.dataset.act === "more"){ el.classList.toggle("open"); b.textContent = el.classList.contains("open") ? "Less" : "More"; }
  if (b.dataset.act === "del"){ await fetch("/__psst/clip/" + id, {method:"DELETE"}); pollClip(); }
};
async function sendClip(){
  const text = $("#clipText").value;
  if (!text.trim()) return;
  const r = await fetch("/__psst/clip", {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify({text})});
  if (r.ok){ $("#clipText").value = ""; await pollClip(); markSeen(); toast("Shared"); } else toast("Could not share");
}
$("#clipSend").onclick = sendClip;
$("#clipText").addEventListener("keydown", e => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)){ e.preventDefault(); sendClip(); } });
$("#clipClear").onclick = async () => { if (clipItems.length && confirm("Clear the shared clipboard for everyone?")){ await fetch("/__psst/clip", {method:"DELETE"}); pollClip(); } };
setInterval(() => { if (document.visibilityState === "visible") pollClip(); }, 2000);

// ---------------------------------------------------------------- connect / QR
$("#qrBtn").onclick = async () => {
  const info = await fetch("/__psst/info").then(r => r.json()).catch(() => ({urls: []}));
  const here = location.pathname;
  const urls = info.urls.length ? info.urls.map(u => u.replace(/\/$/, "") + here) : [location.href];
  $("#qrBox").innerHTML = `<h2>Open on another device</h2><p>Scan with a phone camera, or type the address on another computer on the same network.</p>
    <div class="qr"><img alt="QR code"><code></code></div>` +
    (urls.length > 1 ? `<p style="margin-bottom:6px">Other addresses (if the first doesn't work):</p><div style="display:flex;flex-wrap:wrap;gap:6px;margin-bottom:16px">` +
      urls.map((u, i) => `<button class="btn" data-u="${i}">${esc(new URL(u).host)}</button>`).join("") + `</div>` : "") +
    `<div style="text-align:right"><button class="btn" id="qrClose">Close</button></div>`;
  const show = u => { $("#qrBox img").src = "/__psst/qr.svg?text=" + enc(u); $("#qrBox code").textContent = decodeURI(u); };
  show(urls[0]);
  $("#qrBox").querySelectorAll("[data-u]").forEach(b => b.onclick = () => show(urls[+b.dataset.u]));
  $("#qrModal").classList.add("on");
  $("#qrClose").onclick = () => $("#qrModal").classList.remove("on");
};
$("#qrModal").onclick = e => { if (e.target === $("#qrModal")) $("#qrModal").classList.remove("on"); };

// ---------------------------------------------------------------- keyboard
document.addEventListener("keydown", e => {
  const typing = /^(input|textarea)$/i.test(e.target.tagName);
  if ($("#viewer").classList.contains("on")){
    if (e.key === "Escape") closeViewer();
    else if (e.key === "ArrowLeft") step(-1);
    else if (e.key === "ArrowRight") step(1);
    return;
  }
  if (e.key === "Escape"){
    if ($("#qrModal").classList.contains("on")) $("#qrModal").classList.remove("on");
    else if ($("#clip").classList.contains("on")) closeClip();
    else if (typing) e.target.blur();
    return;
  }
  if (typing) return;
  if (e.key === "/"){ e.preventDefault(); $("#filter").focus(); }
  if (e.key === "Backspace" && state.path !== "/"){ const up = state.path.replace(/[^/]+\/$/, ""); load(up, true); }
});

load(location.pathname, false);
pollClip();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
