"""Install an optional, verified FFmpeg Windows helper into the diary data directory."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import urllib.request
import zipfile

BUILD_URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"


def install(data_dir):
    if os.name != "nt": raise RuntimeError("请通过系统包管理器安装 ffmpeg，程序会从 PATH 自动发现")
    directory = Path(data_dir).resolve() / "tools"
    directory.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(BUILD_URL + ".sha256", timeout=30) as response:
        expected = response.read(2048).decode().strip().split()[0].lower()
    if not re.fullmatch(r"[a-f0-9]{64}", expected): raise RuntimeError("下载校验值无效")
    with tempfile.TemporaryDirectory(prefix="ffmpeg-install-", dir=directory) as name:
        temporary = Path(name).resolve()
        assert temporary.parent == directory
        archive = temporary / "download.zip"
        digest, size = hashlib.sha256(), 0
        with urllib.request.urlopen(BUILD_URL, timeout=60) as source, archive.open("xb") as output:
            while True:
                block = source.read(1024 * 1024)
                if not block: break
                size += len(block)
                if size > 300 * 1024 * 1024: raise RuntimeError("组件压缩包超过预期大小")
                digest.update(block); output.write(block)
        if digest.hexdigest() != expected: raise RuntimeError("组件未通过 SHA-256 校验，未安装")
        selected = {}
        with zipfile.ZipFile(archive) as bundle:
            for member in bundle.infolist():
                path = Path(member.filename)
                if path.name in ("ffmpeg.exe", "ffprobe.exe") and path.parent.name == "bin":
                    selected[path.name] = member
                elif path.name in ("LICENSE", "LICENSE.txt", "README.txt"):
                    selected[path.name] = member
            if not {"ffmpeg.exe", "ffprobe.exe"} <= set(selected): raise RuntimeError("组件压缩包不完整")
            for filename, member in selected.items():
                if member.file_size > 250 * 1024 * 1024: raise RuntimeError("组件文件过大")
                with bundle.open(member) as source, (temporary / filename).open("xb") as output:
                    shutil.copyfileobj(source, output)
            for filename in selected: os.replace(temporary / filename, directory / filename)
        (directory / "ffmpeg-source.json").write_text(json.dumps({"url": BUILD_URL, "sha256": expected}, indent=2), encoding="utf-8")
    return directory


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="安装本机视频兼容预览组件；不修改系统 PATH")
    parser.add_argument("--data-dir", default=str(Path(__file__).resolve().parents[1] / "data"))
    args = parser.parse_args()
    try: print("Video helper installed:", install(args.data_dir))
    except Exception as exc: raise SystemExit(str(exc))
