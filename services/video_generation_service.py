"""图生视频（image-to-video）服务封装。

职责：
- 把「一张参考图 + 一句中文描述」交给 OpenAI-compatible 的视频生成接口，产出一段短视频
  （默认 15 秒，时长由 config.I2V_VIDEO_DURATION_SECONDS 控制）。
- 复用 openai_service 里已建好的 AsyncOpenAI client（带 base_url / 鉴权头），
  绝不在本模块读取或拼接原始 API key。
- 不同中转/供应商的视频接口契约差异很大：有的同步直接回 url，有的回 b64，有的回
  task/job id 需要轮询。本模块把这三种情况都收口在一个函数里，并对「上游根本不支持
  视频接口」做优雅降级——返回 ok=False + 简短中文文案，绝不抛异常炸主流程。

安全：
- 任何日志都不打印 prompt 原文、图片内容、原始 key。只记录 model 名与异常类型。
- 轮询有总超时（config.I2V_POLL_TIMEOUT_SECONDS），避免卡死。

返回统一结构：
    {
      "ok": bool,
      "url": str | None,      # 视频直链（同步或轮询完成）
      "data": bytes | None,   # 当接口回 b64 时，已 decode 成 bytes
      "error": str | None,    # 失败时的简短中文文案
    }

注意：本模块只负责底层调用与契约收口；UX / 路由 / 落屏由调用方处理。
未来若中转的视频接口路径或字段变化，只改这里即可。
"""

from __future__ import annotations

import asyncio
import base64
import os
from typing import Any

from config import (
    I2V_POLL_INTERVAL_SECONDS,
    I2V_POLL_TIMEOUT_SECONDS,
    I2V_VIDEO_DURATION_SECONDS,
    I2V_VIDEO_MODEL,
)
from services.openai_service import client
from utils.logger import setup_logging

logger = setup_logging()


# 上游若不支持视频接口（404 / 405 / 路径不存在）时给调用方的统一文案。
_ERR_UNSUPPORTED = "当前接口暂不支持图生视频，换个时间或联系管理员开通～"
_ERR_GENERIC = "视频生成失败，稍后再试"
_ERR_TIMEOUT = "视频还在渲染、这次没能及时出来，稍后再试一次～"
_ERR_EMPTY = "视频生成返回为空"


def _result(ok: bool, *, url: str | None = None, data: bytes | None = None, error: str | None = None) -> dict[str, Any]:
    return {"ok": ok, "url": url, "data": data, "error": error}


async def _read_image_b64(reference_path: str) -> str | None:
    """把本地参考图读成 base64（不含 data: 前缀）。失败返回 None。"""
    def _read() -> bytes:
        with open(reference_path, "rb") as f:
            return f.read()

    try:
        loop = asyncio.get_running_loop()
        raw = await loop.run_in_executor(None, _read)
        return base64.b64encode(raw).decode("utf-8")
    except Exception as e:
        logger.warning("i2v read reference failed | err=%s", e)
        return None


def _extract_from_payload(payload: Any) -> dict[str, Any] | None:
    """从供应商返回的 JSON 里尽量抠出视频 url 或 b64。

    兼容多种常见字段命名，命中即返回统一结构；都没命中返回 None（让调用方决定下一步）。
    不同接口字段差异大，这里做宽松匹配，新字段只需加进候选列表。
    """
    if not isinstance(payload, dict):
        return None

    # 1) OpenAI images 风格：{"data": [{"url": ...}|{"b64_json": ...}]}
    data_list = payload.get("data")
    if isinstance(data_list, list) and data_list:
        first = data_list[0]
        if isinstance(first, dict):
            url = first.get("url") or first.get("video_url")
            if url:
                return _result(True, url=url)
            b64 = first.get("b64_json") or first.get("b64")
            if b64:
                try:
                    return _result(True, data=base64.b64decode(b64))
                except Exception:
                    return None

    # 2) 扁平 url 字段的多种命名
    for key in ("video_url", "url", "output_url", "result_url"):
        val = payload.get(key)
        if isinstance(val, str) and val.startswith("http"):
            return _result(True, url=val)

    # 3) 嵌套 output / result 里的 url / video
    for container_key in ("output", "result", "data"):
        container = payload.get(container_key)
        if isinstance(container, dict):
            for key in ("video_url", "url", "video"):
                val = container.get(key)
                if isinstance(val, str) and val.startswith("http"):
                    return _result(True, url=val)
        if isinstance(container, list) and container and isinstance(container[0], str):
            if container[0].startswith("http"):
                return _result(True, url=container[0])

    return None


def _extract_task_id(payload: Any) -> str | None:
    """从返回里抠异步任务 id（task/job/request 等命名）。命中返回字符串，否则 None。"""
    if not isinstance(payload, dict):
        return None
    for key in ("task_id", "job_id", "id", "request_id", "taskId", "jobId"):
        val = payload.get(key)
        if isinstance(val, (str, int)) and str(val).strip():
            return str(val).strip()
    # 嵌套 output/result/data 里再找一层
    for container_key in ("output", "result", "data"):
        container = payload.get(container_key)
        if isinstance(container, dict):
            for key in ("task_id", "job_id", "id", "request_id", "taskId", "jobId"):
                val = container.get(key)
                if isinstance(val, (str, int)) and str(val).strip():
                    return str(val).strip()
    return None


def _task_status(payload: Any) -> str:
    """从轮询返回里读任务状态，规范成 succeeded / failed / running 之一。"""
    if not isinstance(payload, dict):
        return "running"
    raw = payload.get("status") or payload.get("state")
    if not raw:
        output = payload.get("output")
        if isinstance(output, dict):
            raw = output.get("task_status") or output.get("status")
    s = str(raw or "").strip().lower()
    if s in ("succeeded", "success", "completed", "complete", "done", "finished", "ok"):
        return "succeeded"
    if s in ("failed", "error", "cancelled", "canceled", "fail"):
        return "failed"
    return "running"


def _is_unsupported_error(err: Exception) -> bool:
    """判断异常是否代表「上游根本没有这个视频接口」（404/405/不支持）。"""
    status = getattr(err, "status_code", None) or getattr(getattr(err, "response", None), "status_code", None)
    if status in (404, 405, 501):
        return True
    text = str(err).lower()
    return any(k in text for k in ("not found", "404", "405", "not support", "unsupported", "no such"))


async def _post_json(path: str, body: dict[str, Any]) -> dict[str, Any]:
    """用 openai client 已鉴权的底层 httpx 通道 POST 一个 JSON。

    复用 client.base_url / 鉴权头，绝不在此读取原始 key。
    返回 {"ok": True, "payload": dict} 或 {"ok": False, "error": str, "unsupported": bool}。
    """
    raw_client = getattr(client, "_client", None)
    base_url = getattr(client, "base_url", None)
    if raw_client is None or base_url is None:
        return {"ok": False, "error": _ERR_UNSUPPORTED, "unsupported": True}

    url = f"{str(base_url).rstrip('/')}/{path.lstrip('/')}"
    headers = {}
    try:
        # client.auth_headers 是 openai SDK 暴露的鉴权头（含 Authorization: Bearer ...）。
        # 我们只透传，不读取/打印其内容。
        ah = getattr(client, "auth_headers", None)
        if isinstance(ah, dict):
            headers.update(ah)
    except Exception:
        pass

    try:
        resp = await raw_client.post(url, json=body, headers=headers or None)
    except Exception as e:
        if _is_unsupported_error(e):
            logger.info("i2v endpoint unsupported | path=%s", path)
            return {"ok": False, "error": _ERR_UNSUPPORTED, "unsupported": True}
        logger.warning("i2v post failed | path=%s | err_type=%s", path, type(e).__name__)
        return {"ok": False, "error": _ERR_GENERIC, "unsupported": False}

    status = getattr(resp, "status_code", 0)
    if status in (404, 405, 501):
        logger.info("i2v endpoint unsupported | path=%s | status=%s", path, status)
        return {"ok": False, "error": _ERR_UNSUPPORTED, "unsupported": True}
    if status >= 400:
        logger.warning("i2v http error | path=%s | status=%s", path, status)
        return {"ok": False, "error": _ERR_GENERIC, "unsupported": False}

    try:
        payload = resp.json()
    except Exception:
        logger.warning("i2v response not json | path=%s", path)
        return {"ok": False, "error": _ERR_GENERIC, "unsupported": False}
    return {"ok": True, "payload": payload}


async def _poll_task(task_id: str) -> dict[str, Any]:
    """对异步任务做最小安全轮询：固定间隔查询，命中 succeeded 抠结果，失败/超时给文案。

    轮询路径同样收口在这里，未来供应商改路径只改一处。
    """
    deadline = asyncio.get_running_loop().time() + I2V_POLL_TIMEOUT_SECONDS
    poll_path = f"videos/tasks/{task_id}"
    while asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(I2V_POLL_INTERVAL_SECONDS)
        raw_client = getattr(client, "_client", None)
        base_url = getattr(client, "base_url", None)
        if raw_client is None or base_url is None:
            return _result(False, error=_ERR_UNSUPPORTED)
        url = f"{str(base_url).rstrip('/')}/{poll_path}"
        headers = {}
        try:
            ah = getattr(client, "auth_headers", None)
            if isinstance(ah, dict):
                headers.update(ah)
        except Exception:
            pass
        try:
            resp = await raw_client.get(url, headers=headers or None)
            payload = resp.json()
        except Exception as e:
            logger.warning("i2v poll failed | err_type=%s", type(e).__name__)
            continue
        status = _task_status(payload)
        if status == "succeeded":
            extracted = _extract_from_payload(payload)
            if extracted:
                return extracted
            return _result(False, error=_ERR_EMPTY)
        if status == "failed":
            logger.info("i2v task failed | tail=%s", task_id[-4:] if len(task_id) >= 4 else "?")
            return _result(False, error=_ERR_GENERIC)
        # running：继续轮询
    logger.info("i2v poll timeout")
    return _result(False, error=_ERR_TIMEOUT)


async def generate_video_from_image(
    prompt: str,
    reference_path: str | None,
    *,
    duration_seconds: int | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    """图生视频主入口：一张参考图 + 一句描述 → 一段短视频。

    - model 为空时默认 I2V_VIDEO_MODEL（wan2.6-i2v-flash）。
    - duration_seconds 为空时默认 I2V_VIDEO_DURATION_SECONDS（15 秒）。
    - 同步返回 url/b64 直接收口；返回 task/job id 时走 _poll_task 最小轮询。
    - 上游不支持视频接口（404/405）→ ok=False + _ERR_UNSUPPORTED，绝不抛异常。

    返回统一结构 {"ok", "url", "data", "error"}。
    """
    text = (prompt or "").strip()
    if not text:
        return _result(False, error="描述为空")
    if not reference_path or not os.path.exists(reference_path):
        logger.info("i2v missing reference image")
        return _result(False, error="没有可用的参考图，先发一张照片～")

    use_model = (model or I2V_VIDEO_MODEL)
    duration = int(duration_seconds or I2V_VIDEO_DURATION_SECONDS)

    image_b64 = await _read_image_b64(reference_path)
    if not image_b64:
        return _result(False, error=_ERR_GENERIC)

    body = {
        "model": use_model,
        "prompt": text,
        "image": f"data:image/jpeg;base64,{image_b64}",
        "duration": duration,
    }

    try:
        posted = await _post_json("videos/generations", body)
    except Exception as e:
        logger.warning("i2v generate crashed | err_type=%s", type(e).__name__)
        return _result(False, error=_ERR_GENERIC)

    if not posted.get("ok"):
        return _result(False, error=posted.get("error") or _ERR_GENERIC)

    payload = posted.get("payload")

    # 1) 同步直接出片
    extracted = _extract_from_payload(payload)
    if extracted:
        return extracted

    # 2) 异步任务：拿到 task/job id → 轮询
    task_id = _extract_task_id(payload)
    if task_id:
        return await _poll_task(task_id)

    # 3) 既没结果也没任务 id：当空返回
    logger.info("i2v empty payload | model=%s", use_model)
    return _result(False, error=_ERR_EMPTY)
