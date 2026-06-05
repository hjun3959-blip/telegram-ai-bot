"""R 级互动剧情 —— 可插拔 USDT 支付层。

用户明确决定：直接收 USDT，不走 Telegram Stars / XTR / send_invoice / pre_checkout_query。
解锁模型："每深入一个阶段付费一次"，金额来自 config.RSTORY_STAGE_PRICES_USDT（集中可配）。

本模块定义可插拔抽象，便于后续替换为真实链上渠道（TRC20 等）而不动 FSM / store / 路由：

    PaymentProvider（抽象基类）
      ├─ create_charge(user_id, character, stage, usdt_amount) -> ChargeInfo
      └─ confirm_charge(charge_id) -> bool          # 查询/确认订单是否已支付

    MockUSDTProvider（默认实现）
      - create_charge：返回模拟 charge（占位地址/备注）。
      - mark_paid(charge_id)：测试/演示用，手动把订单标记为"已支付"。
      - confirm_charge：若被 mark_paid 过则返回 True。

编排函数（与具体 provider 解耦，FSM/store 在这里被串起来）：
- create_unlock_charge：已解锁则不重复收费；否则按阶段定价创建订单 + 写 pending 记录。
- confirm_unlock：确认支付 → 写解锁记录（幂等）→ 推进 FSM 到该阶段入口。

provider 选择由 config.RSTORY_PAYMENT_PROVIDER 决定，get_provider() 返回单例。
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass

import aiohttp

from config import (
    OXAPAY_API_BASE,
    OXAPAY_CALLBACK_BASE_URL,
    OXAPAY_CALLBACK_PATH,
    OXAPAY_HTTP_TIMEOUT_SECONDS,
    OXAPAY_INVOICE_LIFETIME_MIN,
    OXAPAY_MERCHANT_API_KEY,
    OXAPAY_RETURN_URL,
    OXAPAY_SANDBOX,
    RSTORY_PAYMENT_PROVIDER,
    RSTORY_STAGE_PRICES_USDT,
    RSTORY_USDT_RECEIVE_ADDRESS,
)
from services import rstory_fsm_service as fsm
from services import rstory_store as store
from utils.logger import setup_logging

logger = setup_logging()


@dataclass
class ChargeInfo:
    """创建订单后返回给上层的支付信息（占位字段，真实渠道会填真实地址/二维码/链接）。"""

    charge_id: str
    usdt_amount: float
    pay_address: str
    pay_info: str  # 备注/说明/二维码链接占位
    # 真实渠道（OxaPay）额外字段：支付页链接 + 渠道侧会话 ID。Mock 留空。
    payment_url: str | None = None
    track_id: str | None = None


def stage_price_usdt(stage: int) -> float:
    """查阶段定价（USDT）。未知阶段抛 ValueError，避免静默 0 元解锁。"""
    if stage not in RSTORY_STAGE_PRICES_USDT:
        raise ValueError(f"no price configured for stage {stage}")
    return RSTORY_STAGE_PRICES_USDT[stage]


class PaymentProvider(ABC):
    """支付渠道抽象基类。真实渠道（TRC20 等）实现这两个方法即可接入。"""

    name: str = "abstract"

    @abstractmethod
    async def create_charge(
        self, *, user_id: int | str, character: str, stage: int, usdt_amount: float
    ) -> ChargeInfo:
        """创建一笔收款订单，返回 charge_id 与支付信息。不写存储（由编排层写）。"""

    @abstractmethod
    async def confirm_charge(self, charge_id: str) -> bool:
        """查询/确认订单是否已收到款项。已支付返回 True。"""


class MockUSDTProvider(PaymentProvider):
    """默认 Mock 实现：可手动标记已支付，用于跑通完整解锁流程（无需真实链上）。"""

    name = "mock"

    def __init__(self) -> None:
        # charge_id -> 是否已被手动标记支付
        self._paid: set[str] = set()

    async def create_charge(
        self, *, user_id: int | str, character: str, stage: int, usdt_amount: float
    ) -> ChargeInfo:
        charge_id = f"mock_{uuid.uuid4().hex[:16]}"
        address = RSTORY_USDT_RECEIVE_ADDRESS or "TXmockUSDTaddressPLACEHOLDER"
        pay_info = (
            f"[MOCK] 请向 {address} 转账 {usdt_amount} USDT（TRC20 占位）；"
            f"订单 {charge_id}。测试环境可调用 mark_paid 模拟到账。"
        )
        return ChargeInfo(
            charge_id=charge_id,
            usdt_amount=usdt_amount,
            pay_address=address,
            pay_info=pay_info,
        )

    def mark_paid(self, charge_id: str) -> None:
        """测试/演示用：手动把订单标记为已支付。真实 provider 不需要这个方法。"""
        self._paid.add(charge_id)

    async def confirm_charge(self, charge_id: str) -> bool:
        return charge_id in self._paid


class OxaPayError(Exception):
    """OxaPay 渠道调用错误（HTTP / 业务码非成功）。不含密钥信息，可安全上抛/记录。"""


class OxaPayProvider(PaymentProvider):
    """真实支付渠道：OxaPay 加密货币收款（USDT，按 USD 计价）。

    - create_charge：POST {API_BASE}/payment/invoice 生成发票，落库 track_id + payment_url。
      认证用请求头 merchant_api_key（绝不进日志）。
    - 到账确认主路径走 Webhook（services/rstory_webhook.py），不在这里轮询。
    - confirm_charge：以本地订单状态为准（Webhook 已把 paid 写库）；可选地未来接 inquiry 兜底。

    上线 checklist（用户拿到真实 key / 回调域名后）：
      1) 设 OXAPAY_MERCHANT_API_KEY = 真实 merchant key
      2) 设 OXAPAY_CALLBACK_BASE_URL = 公网 HTTPS 基址（OxaPay 不回调私网/localhost）
      3) OXAPAY_SANDBOX=false（关沙盒）
      4) RSTORY_PAYMENT_PROVIDER=oxapay
      5) 在 OxaPay 后台/创建发票参数确认 callback_url 与本服务一致
    """

    name = "oxapay"

    def __init__(self) -> None:
        self._api_base = OXAPAY_API_BASE.rstrip("/")
        self._merchant_key = OXAPAY_MERCHANT_API_KEY
        self._timeout = aiohttp.ClientTimeout(total=OXAPAY_HTTP_TIMEOUT_SECONDS)

    def _callback_url(self) -> str | None:
        base = (OXAPAY_CALLBACK_BASE_URL or "").rstrip("/")
        if not base:
            return None
        path = OXAPAY_CALLBACK_PATH if OXAPAY_CALLBACK_PATH.startswith("/") else "/" + OXAPAY_CALLBACK_PATH
        return base + path

    def _build_invoice_payload(self, *, charge_id: str, usdt_amount: float) -> dict:
        """构造创建发票请求体。order_id 用我方 charge_id 对账；currency=USD（USDT≈USD）。"""
        payload: dict[str, object] = {
            "amount": float(usdt_amount),
            "currency": "USD",
            "lifetime": int(OXAPAY_INVOICE_LIFETIME_MIN),
            "order_id": charge_id,
            "sandbox": bool(OXAPAY_SANDBOX),
        }
        callback_url = self._callback_url()
        if callback_url:
            payload["callback_url"] = callback_url
        if OXAPAY_RETURN_URL:
            payload["return_url"] = OXAPAY_RETURN_URL
        return payload

    async def create_charge(
        self, *, user_id: int | str, character: str, stage: int, usdt_amount: float
    ) -> ChargeInfo:
        if not self._merchant_key:
            raise OxaPayError("OXAPAY_MERCHANT_API_KEY 未配置，无法创建发票")

        charge_id = f"oxapay_{uuid.uuid4().hex[:20]}"
        payload = self._build_invoice_payload(charge_id=charge_id, usdt_amount=usdt_amount)
        url = f"{self._api_base}/payment/invoice"
        headers = {
            "merchant_api_key": self._merchant_key,
            "Content-Type": "application/json",
        }

        try:
            async with aiohttp.ClientSession(timeout=self._timeout) as session:
                async with session.post(url, json=payload, headers=headers) as resp:
                    text = await resp.text()
                    if resp.status >= 400:
                        # 不记录 headers（含 key）；只记录状态码与截断 body。
                        logger.warning(
                            "oxapay invoice http error | status=%s | charge=%s | body=%.300s",
                            resp.status, charge_id, text,
                        )
                        raise OxaPayError(f"OxaPay invoice HTTP {resp.status}")
                    try:
                        body = await resp.json(content_type=None)
                    except Exception as e:
                        raise OxaPayError(f"OxaPay invoice 非 JSON 响应: {e}") from e
        except aiohttp.ClientError as e:
            logger.warning("oxapay invoice request failed | charge=%s | err=%s", charge_id, e)
            raise OxaPayError(f"OxaPay invoice 请求失败: {e}") from e

        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, dict):
            # 某些版本可能把字段平铺在顶层；兼容回退。
            data = body if isinstance(body, dict) else {}
        track_id = data.get("track_id")
        payment_url = data.get("payment_url")
        track_id = str(track_id) if track_id is not None else None
        payment_url = str(payment_url) if payment_url is not None else None
        if not payment_url:
            raise OxaPayError("OxaPay invoice 响应缺少 payment_url")

        logger.info(
            "oxapay invoice created | charge=%s | track=%s | sandbox=%s",
            charge_id, track_id, OXAPAY_SANDBOX,
        )
        pay_info = (
            f"请点击链接用 USDT 支付 {usdt_amount} USD：{payment_url}\n"
            f"订单 {charge_id}。支付完成后会自动解锁（OxaPay 回调确认）。"
        )
        return ChargeInfo(
            charge_id=charge_id,
            usdt_amount=usdt_amount,
            pay_address=payment_url,
            pay_info=pay_info,
            payment_url=payment_url,
            track_id=track_id,
        )

    async def confirm_charge(self, charge_id: str) -> bool:
        """到账以 Webhook 写库为准；这里读本地订单状态判断是否已 paid/confirmed。"""
        charge = await store.get_charge(charge_id)
        if charge is None:
            return False
        return charge.status in (store.CHARGE_PAID, store.CHARGE_CONFIRMED)


# ---------------- provider 选择（单例）----------------

_PROVIDER_REGISTRY: dict[str, type[PaymentProvider]] = {
    "mock": MockUSDTProvider,
    "oxapay": OxaPayProvider,
}

_provider_singleton: PaymentProvider | None = None


def get_provider() -> PaymentProvider:
    """返回当前配置的 provider 单例。未知配置回落到 Mock 并告警。"""
    global _provider_singleton
    if _provider_singleton is not None:
        return _provider_singleton
    cls = _PROVIDER_REGISTRY.get(RSTORY_PAYMENT_PROVIDER)
    if cls is None:
        logger.warning(
            "unknown RSTORY_PAYMENT_PROVIDER=%s, falling back to mock",
            RSTORY_PAYMENT_PROVIDER,
        )
        cls = MockUSDTProvider
    _provider_singleton = cls()
    return _provider_singleton


def set_provider(provider: PaymentProvider) -> None:
    """覆盖当前 provider（测试 / 运行期切换用）。"""
    global _provider_singleton
    _provider_singleton = provider


# ---------------- 编排：创建解锁订单 / 确认解锁 ----------------

@dataclass
class UnlockChargeResult:
    """创建解锁订单的结果。already_unlocked=True 时 charge 为 None（不重复收费）。"""

    already_unlocked: bool
    charge: ChargeInfo | None = None
    stage: int | None = None


async def create_unlock_charge(
    user_id: int | str, character: str, stage: int, *, provider: PaymentProvider | None = None
) -> UnlockChargeResult:
    """为"解锁某阶段"创建订单。

    幂等保护：若该阶段已解锁，直接返回 already_unlocked=True，不创建订单、不收费。
    否则按集中定价创建订单，并写一条 pending 支付记录到独立存储。
    """
    provider = provider or get_provider()
    if await store.is_stage_unlocked(user_id, character, stage):
        return UnlockChargeResult(already_unlocked=True, stage=stage)

    amount = stage_price_usdt(stage)
    info = await provider.create_charge(
        user_id=user_id, character=character, stage=stage, usdt_amount=amount
    )
    await store.create_charge_record(
        charge_id=info.charge_id,
        user_id=user_id,
        character=character,
        stage=stage,
        usdt_amount=amount,
        provider=provider.name,
        pay_address=info.pay_address,
        pay_info=info.pay_info,
        track_id=info.track_id,
        payment_url=info.payment_url,
    )
    logger.info(
        "rstory charge created | uid=%s | char=%s | stage=%s | amount=%s | charge=%s",
        user_id, character, stage, amount, info.charge_id,
    )
    return UnlockChargeResult(already_unlocked=False, charge=info, stage=stage)


@dataclass
class ConfirmResult:
    """确认解锁的结果。"""

    ok: bool  # 支付是否已确认
    unlocked_now: bool = False  # 本次是否新增了解锁记录（幂等：已解锁则 False）
    stage: int | None = None
    state: fsm.StateView | None = None  # 解锁成功后 FSM 推进到的新状态
    message: str = ""


async def confirm_unlock(
    charge_id: str, *, provider: PaymentProvider | None = None
) -> ConfirmResult:
    """确认某订单是否已支付；已支付则写解锁记录（幂等）并推进 FSM 到该阶段入口。

    完整链路：确认支付 → 写解锁记录 → 推进 FSM。任一前置不满足都安全短路。
    """
    provider = provider or get_provider()
    charge = await store.get_charge(charge_id)
    if charge is None:
        return ConfirmResult(ok=False, message="订单不存在。")

    paid = await provider.confirm_charge(charge_id)
    if not paid:
        return ConfirmResult(ok=False, stage=charge.stage, message="尚未收到付款。")

    # 标记订单已确认（幂等：重复确认只是覆盖同状态）
    await store.update_charge_status(charge_id, store.CHARGE_CONFIRMED)

    # 写解锁记录（幂等：已解锁返回 False，不重复收费/不重复解锁）
    unlocked_now = await store.record_unlock(
        charge.user_id, charge.character, charge.stage, charge_id
    )

    # 推进 FSM 到该阶段入口
    state = await fsm.enter_stage(charge.user_id, charge.character, charge.stage)
    logger.info(
        "rstory unlock confirmed | uid=%s | char=%s | stage=%s | new=%s | charge=%s",
        charge.user_id, charge.character, charge.stage, unlocked_now, charge_id,
    )
    return ConfirmResult(
        ok=True,
        unlocked_now=unlocked_now,
        stage=charge.stage,
        state=state,
        message="解锁成功。" if unlocked_now else "该阶段此前已解锁。",
    )


async def settle_paid_charge(charge: store.Charge) -> ConfirmResult:
    """Webhook 已验签确认到账后，结算一笔订单：置 paid → 写解锁 → 推进 FSM。

    与 confirm_unlock 的区别：到账事实由 Webhook（HMAC 验签）给出，这里不再调
    provider.confirm_charge。全程复用 store 的幂等机制：
    - update_charge_status(paid)：confirmed_at 用 COALESCE 不覆盖，重复回调不刷新时间。
    - record_unlock：已解锁返回 False，重复回调不重复解锁、不重复加记录。
    调用方（webhook）应保证 charge 非 None 且金额/币种已校验。
    """
    await store.update_charge_status(charge.charge_id, store.CHARGE_PAID)
    unlocked_now = await store.record_unlock(
        charge.user_id, charge.character, charge.stage, charge.charge_id
    )
    state = await fsm.enter_stage(charge.user_id, charge.character, charge.stage)
    logger.info(
        "rstory unlock via webhook | uid=%s | char=%s | stage=%s | new=%s | charge=%s",
        charge.user_id, charge.character, charge.stage, unlocked_now, charge.charge_id,
    )
    return ConfirmResult(
        ok=True,
        unlocked_now=unlocked_now,
        stage=charge.stage,
        state=state,
        message="解锁成功。" if unlocked_now else "该阶段此前已解锁。",
    )
