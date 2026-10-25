"""Authenticated local media streaming. Browser players use a path-scoped HttpOnly cookie."""
import os
import re
import tempfile
import urllib.parse
import zipfile
from http.cookies import SimpleCookie

from app.media import MAX_ARCHIVE, BLOCK


class MediaHandlerMixin:
    def media_cookie(self):
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
            name = "ShiguangMedia" + str(self.server.server_port)
            return cookie[name].value if name in cookie else ""
        except Exception:
            return ""

    def stream_file(self, path, mime, filename=None, ranges=False):
        size = path.stat().st_size
        start, end, status = 0, size - 1, 200
        requested = self.headers.get("Range") if ranges else None
        if requested:
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", requested)
            if match and (match[1] or match[2]):
                if not match[1]: start = max(0, size - int(match[2]))
                else: start = int(match[1])
                if match[1] and match[2]: end = min(end, int(match[2]))
            if not match or not (match[1] or match[2]) or start > end or start >= size or requested == "bytes=-0":
                self.send_response(416)
                self.send_header("Content-Range", "bytes */" + str(size))
                self.send_header("Content-Length", "0")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                return
            status = 206
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Connection", "close")
        self.close_connection = True
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'; sandbox")
        if ranges:
            self.send_header("Accept-Ranges", "bytes")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        if filename:
            self.send_header("Content-Disposition", "attachment; filename=shiguang-download; filename*=UTF-8''" + urllib.parse.quote(filename, safe=""))
        self.end_headers()
        if self.command == "HEAD": return
        try:
            with path.open("rb") as source:
                source.seek(start)
                remaining = end - start + 1
                while remaining:
                    block = source.read(min(BLOCK, remaining))
                    if not block: break
                    self.wfile.write(block)
                    remaining -= len(block)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def read_media(self, path, query):
        store = self.app.store
        if path == "/api/media/archive":
            archive = store.portable_archive()
            try:
                return self.stream_file(archive, "application/zip", "拾光完整备份.zip")
            finally:
                archive.unlink(missing_ok=True)
        identifier = path.removeprefix("/api/media/")
        if not re.fullmatch(r"[a-f0-9]{32}", identifier):
            return self.send({"error": "附件已不存在"}, 404)
        try:
            row, target = store.media_file(identifier)
        except FileNotFoundError as exc:
            return self.send({"error": str(exc)}, 404)
        filename = store.media_download_name(row, query.get("entry"), query.get("revision")) if query.get("download") else None
        return self.stream_file(target, row["mime"], filename, ranges=True)

    def raw_media(self, path, query):
        self.close_connection = True  # Partial/rejected bodies must never become another request.
        if self.headers.get("Transfer-Encoding"):
            raise ValueError("请选择文件后直接上传")
        length = int(self.headers.get("Content-Length", "0"))
        self.connection.settimeout(60)
        store = self.app.store
        if path == "/api/media/upload":
            return self.send({"attachment": store.upload_media(self.rfile, length, query.get("name", "附件"))})
        if not 0 < length <= MAX_ARCHIVE:
            raise ValueError("完整备份最大 16 GB")
        folder = store.directory / "media-incoming"
        folder.mkdir(exist_ok=True)
        fd, name = tempfile.mkstemp(suffix=".zip", dir=folder)
        try:
            with os.fdopen(fd, "wb") as output:
                remaining = length
                while remaining:
                    block = self.rfile.read(min(BLOCK, remaining))
                    if not block: raise ValueError("备份上传未完成")
                    output.write(block); remaining -= len(block)
            try:
                result = store.preview_archive(name)
            except (zipfile.BadZipFile, KeyError, UnicodeError, RuntimeError) as exc:
                raise ValueError("备份 ZIP 已损坏或不受支持") from exc
            return self.send(result)
        finally:
            os.unlink(name)
