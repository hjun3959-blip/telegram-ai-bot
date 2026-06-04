import asyncio
import base64
import io
import os
import shutil
from functools import partial
from pathlib import Path

from PIL import Image


# 媒体临时文件根目录。
# 生产环境里服务用户对项目根目录没有写权限，导致 photo/voice/video 处理时
# 在根目录直接 open(temp_*.jpg, "wb") 失败。统一收口到一个受控的 tmp/ 目录，
# 用前保证 mkdir(parents=True, exist_ok=True)；可用 MEDIA_TMP_DIR 覆盖到
# 有写权限的位置（例如 /opt/project_phase1_1_test/tmp 或 /tmp/...）。
_PROJECT_ROOT = Path(__file__).resolve().parent.parent


def media_tmp_root() -> Path:
    raw = (os.environ.get("MEDIA_TMP_DIR") or "").strip()
    return Path(raw) if raw else _PROJECT_ROOT / "tmp"


def media_tmp_path(filename: str) -> str:
    """返回 tmp/ 下的文件绝对路径，并确保父目录存在。"""
    root = media_tmp_root()
    root.mkdir(parents=True, exist_ok=True)
    return str(root / filename)


def media_tmp_dir(name: str) -> str:
    """返回 tmp/ 下的子目录绝对路径，并确保该目录存在。"""
    path = media_tmp_root() / name
    path.mkdir(parents=True, exist_ok=True)
    return str(path)


async def encode_image_to_base64(file_path: str, max_size: int = 800) -> str:
    loop = asyncio.get_running_loop()

    def _compress(path: str) -> bytes:
        with Image.open(path) as img:
            img.thumbnail((max_size, max_size), Image.LANCZOS)
            if img.mode in ("RGBA", "P"):
                img = img.convert("RGB")
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=85)
            return buf.getvalue()

    data = await loop.run_in_executor(None, _compress, file_path)
    return base64.b64encode(data).decode("utf-8")


async def run_ffmpeg(args: list[str]) -> bool:
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg",
        *args,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, _ = await proc.communicate()
    return proc.returncode == 0


async def extract_video_frames(video_path: str, output_dir: str) -> list[str]:
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, partial(os.makedirs, output_dir, exist_ok=True))
    success = await run_ffmpeg(["-i", video_path, "-vf", "fps=1/5", "-frames:v", "6", f"{output_dir}/frame_%03d.jpg"])
    if not success:
        return []
    entries = await loop.run_in_executor(None, os.listdir, output_dir)
    return sorted([os.path.join(output_dir, f) for f in entries if f.endswith(".jpg")])[:6]


async def safe_remove(*paths: str):
    loop = asyncio.get_running_loop()

    def _remove(path: str):
        if path and os.path.exists(path):
            os.remove(path)

    for path in paths:
        try:
            await loop.run_in_executor(None, _remove, path)
        except Exception:
            pass


async def safe_rmtree(dir_path: str):
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, lambda: shutil.rmtree(dir_path, ignore_errors=True))

