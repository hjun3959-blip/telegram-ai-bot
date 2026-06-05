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

from config import (
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


# ---------------- provider 选择（单例）----------------

_PROVIDER_REGISTRY: dict[str, type[PaymentProvider]] = {
    "mock": MockUSDTProvider,
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
