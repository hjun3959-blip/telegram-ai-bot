"""R 级互动剧情 —— OxaPay 支付 Webhook 回调端点。

本仓库是 aiogram polling 架构，没有现成 HTTP server。为了接收 OxaPay 的支付状态
回调，这里附带起一个最小 aiohttp web server（仅暴露一个回调路由 + 一个健康检查），
与 polling 并行运行，互不干扰；app 退出时优雅关闭。

安全设计（验签是第一道闸，失败直接拒绝、不处理、不解锁）：
- OxaPay 用 MERCHANT_API_KEY 作为 HMAC 共享密钥，对**原始 POST body**（raw bytes）做
  HMAC-SHA512，签名放在 HTTP 头 `HMAC`。
- 服务端用同一 key 对 raw body 重算 HMAC-SHA512，与请求头用**恒定时间比较**
  （hmac.compare_digest）。不匹配 → 401，不解析、不解锁。
- 仅处理 type==invoice 的回调（payout 用 PAYOUT_API_KEY，本场景不处理）。

对账与幂等：
- 优先用 order_id（=我方 charge_id）定位订单，回退用 track_id。
- 校验回调金额/币种与订单一致，不一致拒绝（防伪造/串单）。
- status 为 paid（大小写不敏感）时，调用 payment.settle_paid_charge 幂等解锁；
  重复回调不重复解锁、不重复加记录。
- 按 OxaPay 约定：成功处理返回 HTTP 200 且 body 为字符串 "ok"，否则 OxaPay 会重试。

上线 checklist：见 services/rstory_payment.py:OxaPayProvider 顶部说明。
"""

from __future__ import annotations

import hashlib
import hmac
import json

from aiohttp import web

from config import (
    OXAPAY_CALLBACK_PATH,
    OXAPAY_MERCHANT_API_KEY,
    OXAPAY_WEBHOOK_HOST,
    OXAPAY_WEBHOOK_PORT,
)
from services import rstory_payment as payment
from services import rstory_store as store
from utils.logger import setup_logging

logger = setup_logging()


# OxaPay 视为“已到账成功”的 status（大小写不敏感比较）。
_PAID_STATUSES = {"paid"}


def compute_signature(raw_body: bytes, secret: str) -> str:
    """对 raw body 用 secret 做 HMAC-SHA512，返回十六进制小写摘要。

    OxaPay 文档：用 MERCHANT_API_KEY 作 HMAC 密钥，签名放 HTTP 头 `HMAC`。
    """
    return hmac.new(
        secret.encode("utf-8"),
        raw_body,
        hashlib.sha512,
    ).hexdigest()


def verify_signature(raw_body: bytes, header_sig: str | None, secret: str) -> bool:
    """恒定时间校验回调签名。secret/签名缺失一律视为失败（安全优先）。"""
    if not secret or not header_sig:
        return False
    expected = compute_signature(raw_body, secret)
    # compare_digest 接受等长字符串；不同长度时也安全返回 False。
    return hmac.compare_digest(expected, header_sig.strip())


def _amount_matches(charge: store.Charge, payload: dict) -> bool:
    """校验回调金额/币种与订单一致。

    订单按 USD 计价（amount=USDT 数额、currency=USD）。OxaPay 回调里的 amount 一般是
    发票计价金额；币种字段可能是 currency。做宽松但安全的比较：
    - 金额：浮点容差比较（避免 2 vs 2.0 / "2" 之类格式差异）。
    - 币种：若回调带 currency，则必须等于订单计价币种 USD（大小写不敏感）；缺失则不强校验币种。
    """
    raw_amount = payload.get("amount")
    try:
        cb_amount = float(raw_amount)
    except (TypeError, ValueError):
        return False
    if abs(cb_amount - float(charge.usdt_amount)) > 1e-6:
        return False

    currency = payload.get("currency")
    if currency is not None and str(currency).strip().upper() not in {"USD", ""}:
        return False
    return True


async def _locate_charge(payload: dict) -> store.Charge | None:
    """按 order_id（我方 charge_id）优先、track_id 回退定位订单。"""
    order_id = payload.get("order_id")
    if order_id:
        charge = await store.get_charge(str(order_id))
        if charge is not None:
            return charge
    track_id = payload.get("track_id")
    if track_id:
        return await store.get_charge_by_track_id(str(track_id))
    return None


async def process_webhook(
    raw_body: bytes, header_sig: str | None, *, secret: str | None = None
) -> tuple[int, str]:
    """处理一次 Webhook 回调，返回 (http_status, body)。

    纯逻辑（不依赖 aiohttp request），便于单测。流程：
    验签 → 解析 JSON → 仅 invoice → 定位订单 → 校验金额/币种 → paid 则幂等解锁。
    成功（含已处理过的重复回调）返回 (200, "ok")。验签失败返回 (401, ...)。
    """
    secret = OXAPAY_MERCHANT_API_KEY if secret is None else secret

    if not verify_signature(raw_body, header_sig, secret):
        logger.warning("oxapay webhook signature mismatch | bytes=%d", len(raw_body))
        return 401, "invalid signature"

    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as e:
        logger.warning("oxapay webhook bad json | err=%s", e)
        return 400, "bad request"
    if not isinstance(payload, dict):
        return 400, "bad request"

    cb_type = str(payload.get("type", "")).strip().lower()
    if cb_type and cb_type != "invoice":
        # 非 invoice（payout 等）不处理；返回 200 ok 避免 OxaPay 反复重试。
        logger.info("oxapay webhook ignored non-invoice | type=%s", cb_type)
        return 200, "ok"

    charge = await _locate_charge(payload)
    if charge is None:
        logger.warning(
            "oxapay webhook charge not found | order_id=%s | track_id=%s",
            payload.get("order_id"), payload.get("track_id"),
        )
        # 找不到单：不解锁；返回 200 ok 避免无意义重试（不是签名问题）。
        return 200, "ok"

    if not _amount_matches(charge, payload):
        logger.warning(
            "oxapay webhook amount/currency mismatch | charge=%s | cb_amount=%s | cb_cur=%s",
            charge.charge_id, payload.get("amount"), payload.get("currency"),
        )
        return 400, "amount mismatch"

    status = str(payload.get("status", "")).strip().lower()
    if status not in _PAID_STATUSES:
        # 非 paid（waiting/confirming/failed/expired 等）：记录状态但不解锁。
        if status in {"failed", "expired"}:
            await store.update_charge_status(charge.charge_id, store.CHARGE_FAILED)
        logger.info(
            "oxapay webhook non-paid status | charge=%s | status=%s",
            charge.charge_id, status,
        )
        return 200, "ok"

    result = await payment.settle_paid_charge(charge)
    logger.info(
        "oxapay webhook settled | charge=%s | unlocked_now=%s",
        charge.charge_id, result.unlocked_now,
    )
    return 200, "ok"


async def _handle_webhook(request: web.Request) -> web.Response:
    raw_body = await request.read()
    header_sig = request.headers.get("HMAC")
    status, body = await process_webhook(raw_body, header_sig)
    return web.Response(status=status, text=body)


async def _handle_health(_request: web.Request) -> web.Response:
    return web.Response(status=200, text="ok")


def build_app() -> web.Application:
    """构造仅含回调路由 + 健康检查的最小 aiohttp app。"""
    app = web.Application()
    path = OXAPAY_CALLBACK_PATH if OXAPAY_CALLBACK_PATH.startswith("/") else "/" + OXAPAY_CALLBACK_PATH
    app.router.add_post(path, _handle_webhook)
    app.router.add_get("/healthz", _handle_health)
    return app


async def start_webhook_server() -> web.AppRunner:
    """启动 Webhook HTTP server（与 polling 并行）。返回 runner 供优雅关闭。"""
    app = build_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, OXAPAY_WEBHOOK_HOST, OXAPAY_WEBHOOK_PORT)
    await site.start()
    logger.info(
        "oxapay webhook server started | host=%s | port=%s | path=%s",
        OXAPAY_WEBHOOK_HOST, OXAPAY_WEBHOOK_PORT, OXAPAY_CALLBACK_PATH,
    )
    return runner


async def stop_webhook_server(runner: web.AppRunner | None) -> None:
    """优雅关闭 Webhook server。可重复调用，幂等。"""
    if runner is None:
        return
    try:
        await runner.cleanup()
    except Exception as e:  # noqa: BLE001
        logger.warning("oxapay webhook server cleanup failed | err=%s", e)
