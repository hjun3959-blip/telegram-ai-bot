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

from config import (
    IMAGE_EDIT_FALLBACK_MODELS,
    IMAGE_MODEL,
    IMAGE_TEXT_MODEL,
    TEXT_IMAGE_FALLBACK_MODELS,
    TEXT_IMAGE_MODEL,
)
from services.openai_service import client
from utils.logger import setup_logging

logger = setup_logging()


def _is_retryable_upstream_error(err: Exception) -> bool:
    """判断异常是否值得换一个模型重试（上游饱和 / 模型不存在 / 限流）。

    典型场景：主模型返回 429（Too Many Requests / 上游饱和）或 model_not_found
    （404 / 400 + "model not found"）。这些都说明「这个模型此刻用不了」，换一个
    实际可用的模型很可能就成功；而 401/403（鉴权）/ 5xx（服务端故障）换模型无意义。
    """
    status = getattr(err, "status_code", None) or getattr(getattr(err, "response", None), "status_code", None)
    if status in (429, 404):
        return True
    text = str(err).lower()
    return any(
        k in text
        for k in (
            "model_not_found",
            "model not found",
            "does not exist",
            "no such model",
            "unsupported model",
            "rate limit",
            "too many requests",
            "429",
            "saturat",
            "overload",
        )
    )


def _dedup_models(*groups: Any) -> list[str]:
    """把若干模型名（单个字符串或列表）按出现顺序去重、去空，合成一条尝试链。"""
    out: list[str] = []
    for g in groups:
        candidates = [g] if isinstance(g, str) else list(g or [])
        for m in candidates:
            name = (m or "").strip()
            if name and name not in out:
                out.append(name)
    return out


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

    # 尝试链：调用方指定的 model（或默认 TEXT_IMAGE_MODEL）排第一，
    # 主模型遇到 429 / model_not_found / 上游饱和时，依次降级到实际可用的备用模型。
    primary = (model or TEXT_IMAGE_MODEL)
    model_chain = _dedup_models(primary, TEXT_IMAGE_FALLBACK_MODELS)

    last_error = "图片生成失败，稍后再试"
    for idx, use_model in enumerate(model_chain):
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
            retryable = _is_retryable_upstream_error(e)
            has_more = idx < len(model_chain) - 1
            if retryable and has_more:
                logger.warning(
                    "Image generation failed, trying fallback model | model=%s | err_type=%s",
                    use_model,
                    type(e).__name__,
                )
                continue
            logger.exception("Image generation failed | model=%s | err=%s", use_model, e)
            return {"ok": False, "url": None, "data": None, "error": last_error}

        parsed = _parse_image_response(response)
        if parsed.get("ok"):
            return parsed
        # 解析为空/格式异常：换下一个模型可能恢复，否则返回该错误。
        last_error = parsed.get("error") or last_error
        if idx < len(model_chain) - 1:
            logger.warning(
                "Image generation empty/invalid, trying fallback model | model=%s",
                use_model,
            )
            continue
        return parsed

    return {"ok": False, "url": None, "data": None, "error": last_error}


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

    # edit 尝试链：主 edit 模型排第一，遇到 429 / model_not_found / 上游饱和时
    # 依次降级到实际可用的 edit 模型（如 qwen-image-edit-plus）。全部失败后才降级到
    # 纯文字生图（_fallback）。signature 不匹配/SDK 不支持 → 直接走 text2image。
    edit_chain = _dedup_models(edit_model, IMAGE_EDIT_FALLBACK_MODELS)

    def _open_image() -> Any:
        return open(reference_path, "rb")

    for idx, use_model in enumerate(edit_chain):
        try:
            # 用同步 open + 异步包装；OpenAI SDK 支持文件对象/bytes
            loop = asyncio.get_running_loop()
            image_file = await loop.run_in_executor(None, _open_image)
            try:
                response = await edit_fn(
                    model=use_model,
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
        except (TypeError, AttributeError) as e:
            # SDK / 中转根本不支持 image edit 入参 → 直接降级到纯文字生图。
            logger.warning("images.edit signature mismatch, fallback to generate | err=%s", e)
            return await _fallback()
        except Exception as e:
            retryable = _is_retryable_upstream_error(e)
            has_more = idx < len(edit_chain) - 1
            if retryable and has_more:
                logger.warning(
                    "images.edit failed, trying fallback edit model | model=%s | err_type=%s",
                    use_model,
                    type(e).__name__,
                )
                continue
            # 不可重试，或已用尽 edit 模型：降级到纯文字生图。
            logger.warning("images.edit failed, fallback to generate | err=%s", e)
            return await _fallback()

        parsed = _parse_image_response(response)
        if parsed.get("ok"):
            # 真正走了 image edit（保脸/保姿势的最佳路径）
            parsed.setdefault("fallback_to_text2image", False)
            return parsed
        # 返回空/格式异常：换下一个 edit 模型，用尽后降级到文字生图。
        if idx < len(edit_chain) - 1:
            logger.warning("images.edit empty/invalid, trying fallback edit model | model=%s", use_model)
            continue
        logger.warning("images.edit empty/invalid on all edit models, fallback to generate")
        return await _fallback()

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
