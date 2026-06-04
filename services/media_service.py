import asyncio
import base64
import io
import os
import shutil
from functools import partial

from PIL import Image


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

