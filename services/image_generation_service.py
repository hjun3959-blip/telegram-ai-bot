"""图片生成服务封装。

目的：
- 调用 OpenAI-compatible images/generations 接口
- 兼容两种返回：url 直链 或 b64_json base64
- 返回统一结构：{"ok": bool, "url": str|None, "data": bytes|None, "error": str|None}
- 不读取密钥，复用 openai_service 中已有的 AsyncOpenAI client
- 任何异常都吞掉、转化为 ok=False，避免炸主流程；调用方根据 ok 决定如何回退

注意：
- meme 风格仅是 prompt 拼接，调用方自行处理，本模块只负责底层调用
- size 默认 1024x1024，可由调用方覆盖
"""

from __future__ import annotations

import asyncio
import base64
import os
from typing import Any

from config import IMAGE_MODEL, IMAGE_TEXT_MODEL, TEXT_IMAGE_MODEL
from services.openai_service import client
from utils.logger import setup_logging

logger = setup_logging()


async def generate_image(
    prompt: str,
    size: str = "1024x1024",
    n: int = 1,
    model: str | None = None,
) -> dict[str, Any]:
    """调用图片生成接口（text-to-image）。

    model 为空时默认走 TEXT_IMAGE_MODEL（/img 文字生图，默认 flux-1.1-pro）。
    其它功能（如 plog/magnet 的降级）可显式传入自己的模型，避免互相影响。

    返回:
        {
          "ok": True/False,
          "url": str | None,        # 当模型直接返回 url 时
          "data": bytes | None,     # 当模型返回 b64_json 时，已 decode 成 bytes
          "error": str | None,      # 失败时的简短文案
        }
    """
    text = (prompt or "").strip()
    if not text:
        return {"ok": False, "url": None, "data": None, "error": "prompt 为空"}

    use_model = (model or TEXT_IMAGE_MODEL)
    try:
        # 一些兼容服务不接受 response_format 参数，因此不强制传；
        # 由服务端决定返回 url 还是 b64_json，我们两种都兼容。
        response = await client.images.generate(
            model=use_model,
            prompt=text,
            size=size,
            n=n,
        )
    except Exception as e:
        # 不打印 prompt，只记录模型名与异常类型
        logger.exception("Image generation failed | model=%s | err=%s", use_model, e)
        return {"ok": False, "url": None, "data": None, "error": "图片生成失败，稍后再试"}

    try:
        data_list = getattr(response, "data", None) or []
        if not data_list:
            return {"ok": False, "url": None, "data": None, "error": "图片生成返回为空"}
        first = data_list[0]
        url = getattr(first, "url", None)
        if url:
            return {"ok": True, "url": url, "data": None, "error": None}
        b64 = getattr(first, "b64_json", None)
        if b64:
            try:
                raw = base64.b64decode(b64)
                return {"ok": True, "url": None, "data": raw, "error": None}
            except Exception as decode_err:
                logger.exception("Image b64 decode failed | err=%s", decode_err)
                return {"ok": False, "url": None, "data": None, "error": "图片解析失败"}
        return {"ok": False, "url": None, "data": None, "error": "图片返回格式异常"}
    except Exception as e:
        logger.exception("Image response parse failed | err=%s", e)
        return {"ok": False, "url": None, "data": None, "error": "图片生成失败，稍后再试"}


def _parse_image_response(response: Any) -> dict[str, Any]:
    """统一解析 images.* 返回。"""
    try:
        data_list = getattr(response, "data", None) or []
        if not data_list:
            return {"ok": False, "url": None, "data": None, "error": "图片生成返回为空"}
        first = data_list[0]
        url = getattr(first, "url", None)
        if url:
            return {"ok": True, "url": url, "data": None, "error": None}
        b64 = getattr(first, "b64_json", None)
        if b64:
            try:
                raw = base64.b64decode(b64)
                return {"ok": True, "url": None, "data": raw, "error": None}
            except Exception as decode_err:
                logger.exception("Image b64 decode failed | err=%s", decode_err)
                return {"ok": False, "url": None, "data": None, "error": "图片解析失败"}
        return {"ok": False, "url": None, "data": None, "error": "图片返回格式异常"}
    except Exception as e:
        logger.exception("Image response parse failed | err=%s", e)
        return {"ok": False, "url": None, "data": None, "error": "图片生成失败，稍后再试"}


async def generate_image_from_reference(
    prompt: str,
    reference_path: str | None,
    size: str = "1024x1024",
    model: str | None = None,
    fallback_model: str | None = None,
) -> dict[str, Any]:
    """以一张参考图 + prompt 生成新图（image edit）。

    模型选择：
    - model：image edit 调用使用的模型。为空时默认 IMAGE_MODEL，保持 plog/magnet/y2k/poster
      等既有功能不变；新的「图 + 文字生图/改图」功能显式传 IMAGE_TEXT_MODEL（默认 flux.1-kontext-pro）。
    - fallback_model：降级到 text-to-image 时使用的模型。为空时跟随 model；
      这样既有功能降级仍走 IMAGE_MODEL，新功能降级走 IMAGE_TEXT_MODEL（或调用方指定）。

    兼容降级路径：
    - 优先尝试 client.images.edit(model, image, prompt, size)；这是 OpenAI 官方 SDK
      对 image edit 的入口。
    - 若 SDK / 中转不支持 image edit（AttributeError / TypeError / 上游 4xx），
      自动降级到 generate_image(prompt)，让模型只按风格化排版 prompt 生成。
    - reference_path 为空时，直接走 generate_image。

    返回结构：
      {"ok": bool, "url": str|None, "data": bytes|None, "error": str|None}
    """
    text = (prompt or "").strip()
    if not text:
        return {"ok": False, "url": None, "data": None, "error": "prompt 为空"}

    edit_model = (model or IMAGE_MODEL)
    text2image_model = (fallback_model or edit_model)

    async def _fallback() -> dict[str, Any]:
        out = await generate_image(text, size=size, model=text2image_model)
        if isinstance(out, dict):
            out["fallback_to_text2image"] = True
        return out

    # 没参考图：直接 generate（标记为已降级到 text2image）
    if not reference_path:
        return await _fallback()

    # 参考图不存在：降级
    if not os.path.exists(reference_path):
        logger.warning("reference image missing | path=%s", reference_path)
        return await _fallback()

    edit_fn = getattr(getattr(client, "images", None), "edit", None)
    if edit_fn is None:
        logger.info("client.images.edit not available, fallback to images.generate")
        return await _fallback()

    # 真正尝试 image edit
    try:
        # 用同步 open + 异步包装；OpenAI SDK 支持文件对象/bytes
        def _open_image() -> Any:
            return open(reference_path, "rb")

        loop = asyncio.get_running_loop()
        image_file = await loop.run_in_executor(None, _open_image)
        try:
            response = await edit_fn(
                model=edit_model,
                image=image_file,
                prompt=text,
                size=size,
                n=1,
            )
        finally:
            try:
                image_file.close()
            except Exception:
                pass
        parsed = _parse_image_response(response)
        if isinstance(parsed, dict):
            # 真正走了 image edit（保脸/保姿势的最佳路径）
            parsed.setdefault("fallback_to_text2image", False)
        return parsed
    except (TypeError, AttributeError) as e:
        logger.warning("images.edit signature mismatch, fallback to generate | err=%s", e)
        return await _fallback()
    except Exception as e:
        # 上游不支持 / 4xx：降级
        logger.warning("images.edit failed, fallback to generate | err=%s", e)
        return await _fallback()


async def generate_image_with_instruction(
    instruction: str,
    reference_path: str | None,
    size: str = "1024x1024",
) -> dict[str, Any]:
    """图 + 文字生图/改图：按用户指令编辑/再创作一张参考图。

    固定使用 IMAGE_TEXT_MODEL（默认 flux.1-kontext-pro）。
    - 有参考图：尝试 image edit（kontext 系最佳路径）；中转不支持时降级到
      纯文字生图（仍用 IMAGE_TEXT_MODEL），并标记 fallback_to_text2image=True。
    - 无参考图：直接按指令做纯文字生图（仍用 IMAGE_TEXT_MODEL）。

    返回结构与 generate_image 一致，额外带 fallback_to_text2image 标记。
    """
    return await generate_image_from_reference(
        prompt=instruction,
        reference_path=reference_path,
        size=size,
        model=IMAGE_TEXT_MODEL,
        fallback_model=IMAGE_TEXT_MODEL,
    )
