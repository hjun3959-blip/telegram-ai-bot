"""R 级互动剧情 —— 可插拔 USDT 支付层（融合数据驱动解锁模型）。

用户最终决定：直接收 USDT，不走 Telegram Stars。解锁单位从旧的"角色+阶段"切换到
数据驱动的 **unlock_products（unlock_id）**；USDT 金额取自 unlock_products.usdt_amount
（集中在 DB 种子里配置，可被 config 覆盖）。

支付通道层不变（沿用已落地的 OxaPay 真实渠道 + Webhook 验签），只把"解锁成功"的写入
目标切到 user_unlocks，并让 fsm_transitions 的 payment 跃迁消费解锁状态。

    PaymentProvider（抽象基类）
      ├─ create_charge(user_id, unlock_id, usdt_amount) -> ChargeInfo
      └─ confirm_charge(charge_id) -> bool

    MockUSDTProvider（默认，可 mark_paid 模拟到账）
    OxaPayProvider（真实渠道：POST /payment/invoice，落 track_id/payment_url；到账走 Webhook）

编排：
- create_unlock_charge：已解锁则不重复收费；否则按 unlock_products USDT 价创建订单 + 写 pending。
- settle_paid_charge：Webhook 验签确认到账后，写 user_unlocks（幂等）+ 消费 FSM payment 跃迁。
- confirm_unlock：主动确认订单（Mock / inquiry）后同样写解锁 + 推进。
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
    RSTORY_USDT_PRICE_OVERRIDES,
    RSTORY_USDT_RECEIVE_ADDRESS,
)
from services import rstory_fsm_service as fsm
from services import rstory_store as store
from utils.logger import setup_logging

logger = setup_logging()


@dataclass
class ChargeInfo:
    """创建订单后返回给上层的支付信息。"""

    charge_id: str
    usdt_amount: float
    pay_address: str
    pay_info: str
    payment_url: str | None = None
    track_id: str | None = None


async def unlock_price_usdt(unlock_id: str) -> float:
    """查解锁产品 USDT 定价。config 覆盖优先，否则取 unlock_products.usdt_amount。

    未知产品抛 ValueError，避免静默 0 元解锁。
    """
    if unlock_id in RSTORY_USDT_PRICE_OVERRIDES:
        return RSTORY_USDT_PRICE_OVERRIDES[unlock_id]
    product = await store.get_unlock_product(unlock_id)
    if product is None:
        raise ValueError(f"unknown unlock product: {unlock_id}")
    return product.usdt_amount


class PaymentProvider(ABC):
    """支付渠道抽象基类。真实渠道实现这两个方法即可接入。"""

    name: str = "abstract"

    @abstractmethod
    async def create_charge(
        self, *, user_id: int | str, unlock_id: str, usdt_amount: float
    ) -> ChargeInfo:
        """创建一笔收款订单。不写存储（由编排层写）。"""

    @abstractmethod
    async def confirm_charge(self, charge_id: str) -> bool:
        """查询/确认订单是否已收款。已支付返回 True。"""


class MockUSDTProvider(PaymentProvider):
    """默认 Mock：可手动标记已支付，跑通完整解锁流程（无需真实链上）。"""

    name = "mock"

    def __init__(self) -> None:
        self._paid: set[str] = set()

    async def create_charge(
        self, *, user_id: int | str, unlock_id: str, usdt_amount: float
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
        self._paid.add(charge_id)

    async def confirm_charge(self, charge_id: str) -> bool:
        return charge_id in self._paid


class OxaPayError(Exception):
    """OxaPay 渠道调用错误（HTTP / 业务码非成功）。不含密钥信息，可安全上抛/记录。"""


class OxaPayProvider(PaymentProvider):
    """真实支付渠道：OxaPay 加密货币收款（USDT，按 USD 计价）。

    - create_charge：POST {API_BASE}/payment/invoice 生成发票，返回 track_id + payment_url。
      认证用请求头 merchant_api_key（绝不进日志）。
    - 到账确认主路径走 Webhook（services/rstory_webhook.py），不在这里轮询。
    - confirm_charge：以本地订单状态为准（Webhook 已把 paid 写库）。

    上线 checklist（用户拿到真实 key / 回调域名后）：
      1) OXAPAY_MERCHANT_API_KEY = 真实 merchant key
      2) OXAPAY_CALLBACK_BASE_URL = 公网 HTTPS 基址（OxaPay 不回调私网/localhost）
      3) OXAPAY_SANDBOX=false
      4) RSTORY_PAYMENT_PROVIDER=oxapay
      5) 确认 callback_url 与本服务一致
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
        self, *, user_id: int | str, unlock_id: str, usdt_amount: float
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
                        logger.warning(
                            "oxapay invoice http error | status=%s | charge=%s | body=%.300s",
                            resp.status, charge_id, text,
                        )
                        raise OxaPayError(f"OxaPay invoice HTTP {resp.status}")
                    try:
                        body = await resp.json(content_type=None)
                    except Exception as e:  # noqa: BLE001
                        raise OxaPayError(f"OxaPay invoice 非 JSON 响应: {e}") from e
        except aiohttp.ClientError as e:
            logger.warning("oxapay invoice request failed | charge=%s | err=%s", charge_id, e)
            raise OxaPayError(f"OxaPay invoice 请求失败: {e}") from e

        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, dict):
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
    """返回当前配置的 provider 单例。未知配置回落 Mock 并告警。"""
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
    unlock_id: str | None = None


async def create_unlock_charge(
    user_id: int | str,
    unlock_id: str,
    *,
    provider: PaymentProvider | None = None,
    script_id: str | None = None,
) -> UnlockChargeResult:
    """为"解锁某产品"创建订单。

    幂等保护：若已解锁，直接返回 already_unlocked=True，不创建订单、不收费。
    否则按 unlock_products USDT 价创建订单，并写一条 pending 支付记录。

    script_id：触发支付时玩家所处的剧本。落到订单上，使解锁结算能按正确剧本消费
    payment 跃迁（多剧情线并行进度的隔离前提）。缺省回落 DEFAULT_SCRIPT_ID。
    """
    provider = provider or get_provider()
    script_id = script_id or fsm.DEFAULT_SCRIPT_ID
    if await store.is_unlocked(user_id, unlock_id):
        return UnlockChargeResult(already_unlocked=True, unlock_id=unlock_id)

    amount = await unlock_price_usdt(unlock_id)
    info = await provider.create_charge(
        user_id=user_id, unlock_id=unlock_id, usdt_amount=amount
    )
    await store.create_charge_record(
        charge_id=info.charge_id,
        user_id=user_id,
        unlock_id=unlock_id,
        usdt_amount=amount,
        provider=provider.name,
        pay_address=info.pay_address,
        pay_info=info.pay_info,
        track_id=info.track_id,
        payment_url=info.payment_url,
        script_id=script_id,
    )
    logger.info(
        "rstory charge created | uid=%s | unlock=%s | amount=%s | charge=%s",
        user_id, unlock_id, amount, info.charge_id,
    )
    return UnlockChargeResult(already_unlocked=False, charge=info, unlock_id=unlock_id)


@dataclass
class ConfirmResult:
    """确认解锁的结果。"""

    ok: bool
    unlocked_now: bool = False
    unlock_id: str | None = None
    advance: fsm.AdvanceResult | None = None  # 解锁后消费 payment 跃迁的结果
    message: str = ""


async def _settle_common(charge: store.Charge) -> ConfirmResult:
    """到账后的统一结算：写 user_unlocks（幂等）+ 消费 FSM payment 跃迁。

    payment 转移的 trigger_value 约定为 "<unlock_id>_paid"（见 seed）。结算时用户在某
    剧本的当前状态应是对应 payment_gate；consume_payment 会校验 condition 并跃迁。
    """
    unlocked_now = await store.record_unlock(
        charge.user_id, charge.unlock_id, source=store.UNLOCK_SOURCE_OXAPAY, charge_id=charge.charge_id
    )
    # 按订单记录的剧本消费 payment 跃迁，使双线进度互不串线；旧订单无 script_id 时回落默认剧本。
    settle_script = charge.script_id or fsm.DEFAULT_SCRIPT_ID
    advance = await fsm.consume_payment(
        charge.user_id, settle_script, f"{charge.unlock_id}_paid"
    )
    logger.info(
        "rstory unlock settled | uid=%s | unlock=%s | new=%s | charge=%s | advance=%s",
        charge.user_id, charge.unlock_id, unlocked_now, charge.charge_id, advance.status,
    )
    return ConfirmResult(
        ok=True,
        unlocked_now=unlocked_now,
        unlock_id=charge.unlock_id,
        advance=advance,
        message="解锁成功。" if unlocked_now else "该产品此前已解锁。",
    )


async def confirm_unlock(
    charge_id: str, *, provider: PaymentProvider | None = None
) -> ConfirmResult:
    """主动确认某订单是否已支付；已支付则写解锁记录（幂等）并消费 FSM payment 跃迁。"""
    provider = provider or get_provider()
    charge = await store.get_charge(charge_id)
    if charge is None:
        return ConfirmResult(ok=False, message="订单不存在。")

    paid = await provider.confirm_charge(charge_id)
    if not paid:
        return ConfirmResult(ok=False, unlock_id=charge.unlock_id, message="尚未收到付款。")

    await store.update_charge_status(charge_id, store.CHARGE_CONFIRMED)
    return await _settle_common(charge)


async def settle_paid_charge(charge: store.Charge) -> ConfirmResult:
    """Webhook 已验签确认到账后，结算一笔订单：置 paid → 写解锁 → 消费 FSM payment 跃迁。

    到账事实由 Webhook（HMAC 验签）给出，这里不再调 provider.confirm_charge。全程复用
    store 幂等机制：update_charge_status（COALESCE 不覆盖时间）、record_unlock（已解锁返回 False）。
    """
    await store.update_charge_status(charge.charge_id, store.CHARGE_PAID)
    return await _settle_common(charge)
