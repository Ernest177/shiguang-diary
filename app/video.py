"""Local, disposable H.264 playback copies. Originals never leave this computer."""
from functools import lru_cache
import os
from pathlib import Path
import shutil
import struct
import subprocess
import threading
import uuid

VERSION = "v1"
CONTAINERS = {b"moov", b"trak", b"mdia", b"minf", b"stbl"}


@lru_cache(maxsize=512)
def video_codecs(filename, size, modified):
    """Read only bounded ISO BMFF metadata; never scan/copy the video payload."""
    codecs, remaining = [], 10000
    with Path(filename).open("rb") as stream:
        def walk(start, end, depth=0):
            nonlocal remaining
            if depth > 6: return
            while start + 8 <= end and remaining:
                remaining -= 1
                stream.seek(start); header = stream.read(8)
                if len(header) != 8: return
                length, kind = struct.unpack(">I4s", header)
                overhead = 8
                if length == 1:
                    extended = stream.read(8)
                    if len(extended) != 8: return
                    length, overhead = int.from_bytes(extended, "big"), 16
                if length == 0: length = end - start
                if length < overhead or start + length > end: return
                body = start + overhead
                if kind in CONTAINERS: walk(body, start + length, depth + 1)
                elif kind == b"stsd":
                    position = body + 8
                    for _ in range(16):
                        if position + 8 > start + length: break
                        stream.seek(position); entry = stream.read(8)
                        if len(entry) != 8: break
                        entry_size, codec = struct.unpack(">I4s", entry)
                        if entry_size < 8 or position + entry_size > start + length: break
                        codecs.append(codec.decode("ascii", "replace")); position += entry_size
                start += length
        walk(0, size)
    return tuple(codecs)


def helper(directory):
    bundled = Path(directory) / "tools" / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")
    return str(bundled) if bundled.is_file() else shutil.which("ffmpeg")


def playback_metadata(row, path, directory):
    if not row["mime"].startswith("video/"): return {}
    try: info = path.stat()
    except OSError: return {}
    codecs = video_codecs(str(path), info.st_size, info.st_mtime_ns) if row["extension"] in (".mp4", ".mov") else ()
    required = row["extension"] == ".mov" or bool(set(codecs) & {"hvc1", "hev1", "dvh1", "dvhe"})
    if not required: return {}
    if not helper(directory):
        return {"requires_preview": True, "playback_warning": "此视频需要兼容预览。尚未启用本机视频组件，可先下载原件播放。"}
    url = "/api/media/" + row["id"]
    return {"requires_preview": True, "playback_url": url + "?preview=1", "poster_url": url + "?poster=1"}


def remove_previews(directory, sha):
    folder = Path(directory) / "media-previews" / VERSION
    for extension in (".mp4", ".jpg"):
        try: (folder / (sha + extension)).unlink(missing_ok=True)
        except OSError: pass


class VideoPreviews:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.lock = threading.Lock()

    def ensure(self, row, original):
        if not row["mime"].startswith("video/"): raise ValueError("此附件不是视频")
        with self.lock:
            folder = self.directory / "media-previews" / VERSION
            target, poster = folder / (row["sha256"] + ".mp4"), folder / (row["sha256"] + ".jpg")
            if target.is_file() and target.stat().st_size and poster.is_file() and poster.stat().st_size:
                return target, poster
            executable = helper(self.directory)
            if not executable: raise ValueError("尚未安装视频兼容组件，可先下载原件播放")
            folder.mkdir(parents=True, exist_ok=True)
            temporary = folder / (uuid.uuid4().hex + ".mp4")
            thumbnail = folder / (uuid.uuid4().hex + ".jpg")
            options = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
                       "timeout": 180, "check": True}
            if os.name == "nt": options["creationflags"] = subprocess.CREATE_NO_WINDOW
            common = [executable, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-threads", "2", "-filter_threads", "2"]
            try:
                subprocess.run(common + ["-i", str(original), "-map", "0:v:0", "-map", "0:a?", "-c:v", "libx264",
                    "-preset", "veryfast", "-crf", "20", "-vf", "scale=w='min(iw,1920)':h='min(ih,1920)':force_original_aspect_ratio=decrease:force_divisible_by=2",
                    "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k", "-map_metadata", "-1", "-map_chapters", "-1",
                    "-movflags", "+faststart", "-threads", "2", str(temporary)], **options)
                subprocess.run(common + ["-i", str(temporary), "-frames:v", "1", "-vf", "scale=w='min(iw,960)':h=-2", "-q:v", "3", str(thumbnail)], **options)
                if not temporary.stat().st_size or not thumbnail.stat().st_size: raise ValueError("视频预览为空")
                os.replace(temporary, target); os.replace(thumbnail, poster)
            except (OSError, subprocess.SubprocessError, ValueError):
                raise ValueError("暂时无法生成兼容预览，原件已保留，可下载播放") from None
            finally:
                temporary.unlink(missing_ok=True); thumbnail.unlink(missing_ok=True)
            return target, poster
