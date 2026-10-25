"""Local immutable media, independent layout revisions and portable streamed backups."""
from __future__ import annotations

import hashlib
from collections import defaultdict
import json
import os
from pathlib import Path
import re
import tempfile
import uuid
import zipfile
from datetime import date, datetime, timezone
from urllib.parse import urlencode

MEDIA_TABLES = ("attachments", "media_versions", "entry_media")
MEDIA_SCHEMA = """
CREATE TABLE IF NOT EXISTS attachments(
 id TEXT PRIMARY KEY,sha256 TEXT NOT NULL,size INTEGER NOT NULL,mime TEXT NOT NULL,
 extension TEXT NOT NULL,name TEXT NOT NULL,created_at TEXT NOT NULL,published INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS media_versions(
 entry_id TEXT NOT NULL REFERENCES entries(id) ON DELETE CASCADE,revision INTEGER NOT NULL,
 entry_revision INTEGER NOT NULL,saved_at TEXT NOT NULL,PRIMARY KEY(entry_id,revision),
 FOREIGN KEY(entry_id,entry_revision) REFERENCES entry_versions(entry_id,revision) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS entry_media(
 entry_id TEXT NOT NULL,revision INTEGER NOT NULL,attachment_id TEXT NOT NULL REFERENCES attachments(id),
 position INTEGER NOT NULL,sequence INTEGER NOT NULL,label TEXT NOT NULL DEFAULT '',PRIMARY KEY(entry_id,revision,sequence),
 FOREIGN KEY(entry_id,revision) REFERENCES media_versions(entry_id,revision) ON DELETE CASCADE);
CREATE INDEX IF NOT EXISTS media_attachment_refs ON entry_media(attachment_id);
CREATE UNIQUE INDEX IF NOT EXISTS media_version_attachment ON entry_media(entry_id,revision,attachment_id);
CREATE INDEX IF NOT EXISTS media_objects ON attachments(sha256,extension);
CREATE TABLE IF NOT EXISTS image_name_cache(
 cache_key TEXT PRIMARY KEY,sha256 TEXT NOT NULL,label TEXT NOT NULL,created_at TEXT NOT NULL);
"""
TYPES = {".jpg": "image/jpeg", ".png": "image/png", ".gif": "image/gif", ".webp": "image/webp",
         ".mp4": "video/mp4", ".webm": "video/webm", ".mov": "video/quicktime"}
MAX_FILE = 512 * 1024 * 1024
MAX_IMAGE = 25 * 1024 * 1024
MAX_ARCHIVE = 16 * 1024 * 1024 * 1024
BLOCK = 1024 * 1024


def timestamp():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def sniff(prefix):
    if prefix.startswith(b"\xff\xd8\xff"): return ".jpg"
    if prefix.startswith(b"\x89PNG\r\n\x1a\n"): return ".png"
    if prefix.startswith((b"GIF87a", b"GIF89a")): return ".gif"
    if prefix[:4] == b"RIFF" and prefix[8:12] == b"WEBP": return ".webp"
    if prefix.startswith(b"\x1aE\xdf\xa3") and b"webm" in prefix[:4096].lower(): return ".webm"
    if prefix[4:8] == b"ftyp" and len(prefix) >= 12:
        brand = prefix[8:12]
        if brand == b"qt  ": return ".mov"
        if brand in (b"isom", b"iso2", b"iso4", b"iso5", b"iso6", b"mp41", b"mp42", b"avc1", b"M4V ", b"MSNV", b"dash"):
            return ".mp4"
    raise ValueError("请选择 JPG、PNG、GIF、WebP 图片，或 MP4、WebM、MOV 视频；暂不支持此文件格式")


def object_name(row):
    digest, extension = row.get("sha256"), row.get("extension")
    if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest) or extension not in TYPES:
        raise ValueError("附件标识无效")
    return digest[:2] + "/" + digest + extension


def dated_name(row, journal_day):
    """Derive a human filename from the original name and diary date, never mutate either."""
    if not isinstance(journal_day, str) or date.fromisoformat(journal_day).isoformat() != journal_day:
        raise ValueError("日记日期无效")
    original = row.get("label") + row["extension"] if row.get("label") else row["name"]
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', "_", original).rstrip(". ")
    dot = name.rfind(".")
    stem = (name[:dot] if dot > 0 else name).rstrip(". ")[:110] or "附件"
    if re.fullmatch(r"CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9]", stem.split(".")[0], re.IGNORECASE): stem = "_" + stem
    suffix = "_" + journal_day
    return stem + ("" if stem.endswith(suffix) else suffix) + row["extension"]


def media_label(value):
    if not isinstance(value, str) or len(value) > 60 or re.search(r'[<>:"/\\|?*\x00-\x1f\x7f]', value):
        raise ValueError("图片名称最多 60 个字，不能包含路径或特殊文件名符号")
    return value.strip().rstrip(". ")


def copied(source, target, expected_size=None, expected_hash=None):
    digest, size = hashlib.sha256(), 0
    while True:
        block = source.read(BLOCK)
        if not block: break
        size += len(block)
        if expected_size is not None and size > expected_size:
            raise ValueError("附件大小与清单不一致")
        digest.update(block)
        target.write(block)
    if expected_size is not None and size != expected_size or expected_hash is not None and digest.hexdigest() != expected_hash:
        raise ValueError("附件缺失或校验不一致，原有记录未被替换")
    return size, digest.hexdigest()


class MediaMixin:
    def media_root(self):
        return self.directory / "media"

    def upload_media(self, stream, length, name):
        if not isinstance(length, int) or not 0 < length <= MAX_FILE:
            raise ValueError("单个视频最大 512 MB，图片最大 25 MB")
        name = re.split(r"[/\\]", str(name))[-1]
        name = "".join(c for c in name if ord(c) >= 32)[:200] or "附件"
        incoming = self.directory / "media-incoming"
        incoming.mkdir(exist_ok=True)
        temporary = incoming / (uuid.uuid4().hex + ".part")
        try:
            digest, remaining, prefix = hashlib.sha256(), length, b""
            with temporary.open("xb") as output:
                while remaining:
                    block = stream.read(min(BLOCK, remaining))
                    if not block:
                        raise ValueError("上传未完成，请重新选择文件")
                    if len(prefix) < 4096: prefix += block[:4096 - len(prefix)]
                    digest.update(block); output.write(block); remaining -= len(block)
            extension = sniff(prefix)
            if TYPES[extension].startswith("image/") and length > MAX_IMAGE:
                raise ValueError("图片最大 25 MB，请缩小图片后再添加")
            row = {"id": uuid.uuid4().hex, "sha256": digest.hexdigest(), "size": length,
                   "mime": TYPES[extension], "extension": extension, "name": name, "created_at": timestamp(), "published": 0}
            target = self.media_root() / object_name(row)
            with self.connection() as db:
                target.parent.mkdir(parents=True, exist_ok=True)
                if not target.exists(): os.replace(temporary, target)
                elif target.stat().st_size != length: raise ValueError("本机附件文件异常，请检查存储空间")
                db.execute("INSERT INTO attachments VALUES(?,?,?,?,?,?,?,?)", tuple(row.values()))
            return row
        finally:
            temporary.unlink(missing_ok=True)

    def media_for(self, identifier, db=None, revision=None):
        if db is None:
            with self.connection() as db: return self.media_for(identifier, db, revision)
        head = db.execute("SELECT * FROM media_versions WHERE entry_id=? " +
                          ("AND revision=?" if revision is not None else "ORDER BY revision DESC LIMIT 1"),
                          (identifier, revision) if revision is not None else (identifier,)).fetchone()
        if not head: return {"attachments": [], "media_revision": 0, "media_updated_at": None}
        rows = [dict(r) for r in db.execute("""SELECT a.*,m.position,m.sequence,m.label FROM entry_media m
            JOIN attachments a ON a.id=m.attachment_id WHERE m.entry_id=? AND m.revision=? ORDER BY sequence""", (identifier, head["revision"]))]
        day = db.execute("SELECT journal_day FROM entry_versions WHERE entry_id=? AND revision=?", (identifier, head["entry_revision"])).fetchone()[0]
        for row in rows:
            row.update(entry_id=identifier, media_revision=head["revision"], journal_day=day,
                       display_name=dated_name(row, day),
                       download_url="/api/media/" + row["id"] + "?" + urlencode({"download": 1, "entry": identifier, "revision": head["revision"]}))
        return {"attachments": rows, "media_revision": head["revision"], "media_updated_at": head["saved_at"]}

    def media_download_name(self, row, entry_id=None, revision=None):
        with self.connection() as db:
            if revision is not None and not entry_id: raise ValueError("请选择附件所属的日记")
            if entry_id:
                if not isinstance(entry_id, str) or len(entry_id) > 80: raise ValueError("日记编号无效")
                if revision is None:
                    revision = self.media_for(entry_id, db)["media_revision"]
                elif not isinstance(revision, (int, str)) or not str(revision).isdigit() or len(str(revision)) > 10 or int(revision) < 1:
                    raise ValueError("附件版本无效")
                version = db.execute("""SELECT v.journal_day,a.label FROM entry_media a JOIN media_versions m
                    ON m.entry_id=a.entry_id AND m.revision=a.revision JOIN entry_versions v
                    ON v.entry_id=m.entry_id AND v.revision=m.entry_revision
                    WHERE a.attachment_id=? AND a.entry_id=? AND a.revision=?""", (row["id"], entry_id, int(revision))).fetchone()
                if not version: raise ValueError("此日记版本中没有这个附件，请重新打开记录")
                return dated_name({**row, "label": version[1]}, version[0])
            # Old links lack context. Use a date only when every reference agrees.
            days = {(r[0], r[1]) for r in db.execute("""SELECT DISTINCT v.journal_day,a.label FROM entry_media a JOIN media_versions m
                ON m.entry_id=a.entry_id AND m.revision=a.revision JOIN entry_versions v
                ON v.entry_id=m.entry_id AND v.revision=m.entry_revision WHERE a.attachment_id=?""", (row["id"],))}
            if len(days) == 1:
                day, label = next(iter(days))
                return dated_name({**row, "label": label}, day)
            return row["name"]

    def media_layout(self, values, body, db, previous_labels=None):
        if not isinstance(values, list) or len(values) > 60: raise ValueError("每条日记最多添加 60 个附件")
        result, seen = [], set()
        for value in values:
            if not isinstance(value, dict): raise ValueError("附件格式无效")
            identifier, position = value.get("id"), value.get("position", len(body))
            if not isinstance(identifier, str) or identifier in seen or isinstance(position, bool) or not isinstance(position, int) or not 0 <= position <= len(body):
                raise ValueError("附件位置无效，请重新打开日记后编辑")
            row = db.execute("SELECT * FROM attachments WHERE id=?", (identifier,)).fetchone()
            if not row or not (self.media_root() / object_name(dict(row))).is_file():
                raise ValueError("附件尚未上传完成或已不存在，请重新选择")
            label = value.get("label", (previous_labels or {}).get(identifier, ""))
            result.append({"id": identifier, "position": position, "label": media_label(label)}); seen.add(identifier)
        return result

    def save_media_layout(self, db, identifier, entry_revision, layout):
        previous = self.media_for(identifier, db)
        if not layout and not previous["media_revision"]: return
        revision = previous["media_revision"] + 1
        db.execute("INSERT INTO media_versions VALUES(?,?,?,?)", (identifier, revision, entry_revision, timestamp()))
        for sequence, item in enumerate(layout):
            db.execute("INSERT INTO entry_media VALUES(?,?,?,?,?,?)", (identifier, revision, item["id"], item["position"], sequence, item["label"]))
            db.execute("UPDATE attachments SET published=1 WHERE id=?", (item["id"],))

    def media_file(self, identifier):
        with self.connection() as db:
            row = db.execute("SELECT * FROM attachments WHERE id=? AND (published=0 OR EXISTS(SELECT 1 FROM entry_media WHERE attachment_id=attachments.id))", (identifier,)).fetchone()
            if not row: raise FileNotFoundError("附件已不存在")
            row = dict(row)
            path = self.media_root() / object_name(row)
            if not path.is_file() or path.stat().st_size != row["size"]: raise FileNotFoundError("附件文件缺失，请从完整备份恢复")
            return row, path

    def discard_media(self, identifier):
        with self.lock:
            with self.connection() as db:
                row = db.execute("SELECT * FROM attachments WHERE id=? AND published=0 AND NOT EXISTS(SELECT 1 FROM entry_media WHERE attachment_id=attachments.id)", (identifier,)).fetchone()
                if not row: return
                row = dict(row)
                db.execute("DELETE FROM attachments WHERE id=?", (identifier,))
                db.execute("DELETE FROM image_name_cache WHERE sha256 NOT IN (SELECT sha256 FROM attachments)")
                retained = db.execute("SELECT 1 FROM attachments WHERE sha256=? AND extension=?", (row["sha256"], row["extension"])).fetchone()
            if not retained:
                try: (self.media_root() / object_name(row)).unlink(missing_ok=True)
                except OSError: pass

    def collect_media(self):
        # Current removals remain in history. Only deleting an entire diary releases its originals.
        with self.lock:
            with self.connection() as db:
                rows = [dict(r) for r in db.execute("SELECT * FROM attachments WHERE published=1 AND NOT EXISTS(SELECT 1 FROM entry_media WHERE attachment_id=attachments.id)")]
                db.execute("DELETE FROM attachments WHERE published=1 AND NOT EXISTS(SELECT 1 FROM entry_media WHERE attachment_id=attachments.id)")
                db.execute("DELETE FROM image_name_cache WHERE sha256 NOT IN (SELECT sha256 FROM attachments)")
                removable = [r for r in rows if not db.execute("SELECT 1 FROM attachments WHERE sha256=? AND extension=?", (r["sha256"], r["extension"])).fetchone()]
            # A failed database commit must never delete a still-referenced original.
            for row in removable:
                try: (self.media_root() / object_name(row)).unlink(missing_ok=True)
                except OSError: pass  # File players may still hold the handle; keep the harmless orphan.

    def media_history(self, identifier):
        with self.connection() as db:
            return [{**dict(r), **self.media_for(identifier, db, r["revision"]),
                     "body": db.execute("SELECT body FROM entry_versions WHERE entry_id=? AND revision=?", (identifier, r["entry_revision"])).fetchone()[0]}
                    for r in db.execute("SELECT * FROM media_versions WHERE entry_id=? ORDER BY revision DESC", (identifier,))]

    def validate_media_import(self, tables, root=None):
        attachments = {a["id"]: a for a in tables["attachments"]}
        bodies = {(v["entry_id"], v["revision"]): v["body"] for v in tables["entry_versions"]}
        versions = {(v["entry_id"], v["revision"]): v for v in tables["media_versions"]}
        refs_by_version, heads, backed_versions = defaultdict(list), {}, set()
        for row in attachments.values():
            if not isinstance(row["id"], str) or not re.fullmatch(r"[a-f0-9]{32}", row["id"]): raise ValueError("附件编号无效")
            relative = object_name(row)
            if row["mime"] != TYPES[row["extension"]] or not isinstance(row["size"], int) or isinstance(row["size"], bool) or not 0 < row["size"] <= MAX_FILE:
                raise ValueError("附件清单无效")
            if row["mime"].startswith("image/") and row["size"] > MAX_IMAGE: raise ValueError("备份图片过大")
            if not isinstance(row["name"], str) or not row["name"] or len(row["name"]) > 200 or any(ord(c) < 32 for c in row["name"]) or row["published"] != 1: raise ValueError("附件名称或状态无效")
            datetime.fromisoformat(row["created_at"])
            path = (Path(root) if root else self.media_root()) / relative
            if not path.is_file() or path.stat().st_size != row["size"]: raise ValueError("备份缺少附件原件，请使用包含图片和视频的 ZIP 完整备份")
        for version in versions.values():
            if isinstance(version["revision"], bool) or not isinstance(version["revision"], int) or version["revision"] < 1: raise ValueError("附件版本无效")
            datetime.fromisoformat(version["saved_at"])
            if version["revision"] > heads.get(version["entry_id"], {}).get("revision", 0): heads[version["entry_id"]] = version
        for ref in tables["entry_media"]:
            if media_label(ref["label"]) != ref["label"]: raise ValueError("备份附件名称无效")
            version = versions[(ref["entry_id"], ref["revision"])]
            body = bodies[(ref["entry_id"], version["entry_revision"])]
            if isinstance(ref["position"], bool) or not isinstance(ref["position"], int) or not 0 <= ref["position"] <= len(body) or isinstance(ref["sequence"], bool) or not isinstance(ref["sequence"], int) or ref["sequence"] < 0:
                raise ValueError("备份附件位置无效")
            refs_by_version[(ref["entry_id"], ref["revision"])].append(ref)
            backed_versions.add((ref["entry_id"], version["entry_revision"]))
        for entry in tables["entries"]:
            head = heads.get(entry["id"])
            if head and head["entry_revision"] != entry["revision"]: raise ValueError("当前附件未关联当前文字版本")
            if not entry["body"].strip() and (entry["ai_enabled"] or not head or not refs_by_version[(entry["id"], head["revision"])]):
                raise ValueError("空白记录必须包含附件，且不参与 AI 整理")
        for key, body in bodies.items():
            if not body.strip() and key not in backed_versions:
                raise ValueError("空白历史记录缺少附件")
        for version in versions.values():
            refs = refs_by_version[(version["entry_id"], version["revision"])]
            if len(refs) > 60 or len({r["attachment_id"] for r in refs}) != len(refs) or sorted(r["sequence"] for r in refs) != list(range(len(refs))):
                raise ValueError("备份附件顺序无效")

    def restore_media_files(self, bundle, source_root):
        # Validate every source before copying anything; commit each immutable file atomically.
        self.validate_media_import(bundle["tables"], source_root)
        for row in bundle["tables"]["attachments"]:
            target = self.media_root() / object_name(row)
            source = Path(source_root) / object_name(row)
            if target.resolve() == source.resolve(): continue
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_suffix(target.suffix + "." + uuid.uuid4().hex + ".tmp")
            try:
                with source.open("rb") as reader, temporary.open("xb") as writer:
                    copied(reader, writer, row["size"], row["sha256"])
                os.replace(temporary, target)
            finally: temporary.unlink(missing_ok=True)

    def mirror_media(self, folder, bundle=None):
        bundle = bundle or self.export()
        for row in bundle["tables"]["attachments"]:
            target = Path(folder) / "media" / object_name(row)
            if target.is_file() and target.stat().st_size == row["size"]: continue
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_suffix(target.suffix + "." + uuid.uuid4().hex + ".tmp")
            try:
                with (self.media_root() / object_name(row)).open("rb") as source, temporary.open("xb") as output:
                    copied(source, output, row["size"], row["sha256"])
                os.replace(temporary, target)
            finally: temporary.unlink(missing_ok=True)

    def portable_archive(self):
        folder = self.directory / "media-incoming"
        folder.mkdir(exist_ok=True)
        target = folder / (uuid.uuid4().hex + ".zip")
        try:
            with self.lock:
                bundle = self.export()
                manifest = json.dumps(bundle, ensure_ascii=False).encode("utf-8")
                if len(manifest) > 30_000_000: raise ValueError("日记清单超过便携备份的 30 MB 上限，请先使用本地快照备份")
                objects = {object_name(a): a for a in bundle["tables"]["attachments"]}
                if sum(a["size"] for a in objects.values()) + len(manifest) + len(objects) * 512 + 65536 > MAX_ARCHIVE:
                    raise ValueError("附件超过 ZIP 完整备份的 16 GB 上限，请保管本地数据库及 media 原件目录")
                with zipfile.ZipFile(target, "w", allowZip64=True) as archive:
                    archive.writestr("diary.json", manifest, compress_type=zipfile.ZIP_DEFLATED)
                    for relative, row in objects.items():
                        with (self.media_root() / relative).open("rb") as source, archive.open("media/" + relative, "w", force_zip64=True) as output:
                            copied(source, output, row["size"], row["sha256"])
            return target
        except BaseException:
            target.unlink(missing_ok=True)
            raise

    def preview_archive(self, source):
        root = self.directory / "media-incoming"
        root.mkdir(exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix="restore-", dir=root)
        try:
            with zipfile.ZipFile(source) as archive:
                names = archive.namelist()
                if len(names) > 100001 or len(names) != len(set(names)) or "diary.json" not in names or archive.getinfo("diary.json").file_size > 30_000_000:
                    raise ValueError("备份目录或清单无效")
                bundle = self.normalize_bundle(json.loads(archive.read("diary.json")))
                objects = {"media/" + object_name(a): a for a in bundle["tables"]["attachments"]}
                if set(names) != {"diary.json", *objects} or sum(i.file_size for i in archive.infolist()) > MAX_ARCHIVE:
                    raise ValueError("备份包含无效路径或超出大小限制")
                for name, row in objects.items():
                    info = archive.getinfo(name)
                    if info.file_size != row["size"] or info.flag_bits & 1 or (info.external_attr >> 16) & 0o170000 == 0o120000:
                        raise ValueError("附件备份清单与原件不一致")
                    target = Path(temporary.name) / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(name) as reader, target.open("xb") as writer:
                        copied(reader, writer, row["size"], row["sha256"])
                    with target.open("rb") as reader:
                        if sniff(reader.read(4096)) != row["extension"]: raise ValueError("附件格式与备份不一致")
            preview = self.validate_import(bundle, media_root=Path(temporary.name) / "media")
            ticket = uuid.uuid4().hex
            with self.lock:
                if not hasattr(self, "media_imports"): self.media_imports = {}
                # Keep only a small number of explicit preview sessions on disk.
                if len(self.media_imports) >= 3:
                    old = next(iter(self.media_imports)); self.media_imports.pop(old)[0].cleanup()
                self.media_imports[ticket] = (temporary, bundle)
            return {"ticket": ticket, "preview": preview}
        except (KeyError, TypeError, AttributeError) as exc:
            temporary.cleanup()
            raise ValueError("备份附件清单无效") from exc
        except BaseException:
            temporary.cleanup()
            raise

    def restore_archive(self, ticket, replace=False):
        if not isinstance(ticket, str): raise ValueError("请先选择完整备份并预览")
        with self.lock:
            saved = getattr(self, "media_imports", {}).get(ticket)
            if not saved: raise ValueError("备份预览已过期，请重新选择文件")
            temporary, bundle = saved
            self.restore_media_files(bundle, Path(temporary.name) / "media")
            result = self.import_data(bundle, replace=replace)
            self.media_imports.pop(ticket)[0].cleanup()
        return result
