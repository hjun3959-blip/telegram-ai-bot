"""Smoke test：R 级互动剧情系统 FSM 引擎骨架。

覆盖（不联网；DB 用临时文件，剧情库走独立 RSTORY_DB_PATH）：
1) 独立存储幂等建表 + 进度读写（upsert）
2) FSM 状态推进：合法转移按图前进
3) FSM 非法转移：返回 invalid，不改变状态
4) FSM 阶段边界未解锁：返回 needs_unlock，不推进
5) 解锁定价：三阶段 USDT 金额来自集中配置（2 / 3 / 5）
6) 抽象支付完整流程：create_unlock_charge → confirm_unlock → 写解锁 + 推进 FSM
7) 解锁后阶段边界可推进进入新阶段
8) 解锁幂等：已解锁阶段再创建订单 -> already_unlocked，不重复收费；
   重复 confirm 不重复新增解锁记录
9) 支付记录状态流转：pending -> confirmed，confirmed_at 落库
10) provider 可插拔：默认 Mock；set_provider 可替换；自定义 provider 跑通流程
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


async def main() -> None:
    tmpdir = tempfile.mkdtemp(prefix="rstory_smoke_")
    db_path = os.path.join(tmpdir, "rstory.sqlite3")
    # 主库与剧情库都指向临时文件（剧情库用独立 env，验证独立性）
    os.environ["BOT_DB_PATH"] = os.path.join(tmpdir, "main.sqlite3")
    os.environ["RSTORY_DB_PATH"] = db_path
    os.environ.setdefault("RSTORY_PAYMENT_PROVIDER", "mock")

    for mod in (
        "config",
        "services.rstory_content",
        "services.rstory_store",
        "services.rstory_fsm_service",
        "services.rstory_payment",
    ):
        sys.modules.pop(mod, None)

    import config  # noqa: F401
    from services import rstory_content as content
    from services import rstory_fsm_service as fsm
    from services import rstory_payment as payment
    from services import rstory_store as store

    await store.init_store()
    character = content.DEFAULT_CHARACTER_ID
    uid = 90001

    # ---------- 1) 独立存储幂等建表 + 进度 upsert ----------
    await store.init_store()  # 第二次调用应幂等，不报错
    assert await store.get_progress(uid, character) is None
    await store.set_progress(uid, character, 1, "s1_intro")
    prog = await store.get_progress(uid, character)
    assert prog is not None and prog.stage == 1 and prog.node == "s1_intro"
    # upsert：覆盖同一 (user, character)
    await store.set_progress(uid, character, 1, "s1_talk")
    prog2 = await store.get_progress(uid, character)
    assert prog2.node == "s1_talk", f"upsert 应覆盖 node，得到 {prog2.node}"
    print("[ok] 独立存储幂等建表 + 进度 upsert 读写")

    # ---------- 2) FSM 合法转移推进 ----------
    # 重新开始一个干净用户
    uid = 90002
    state = await fsm.start_story(uid, character)
    assert state.stage == 1 and state.node_id == "s1_intro"
    r = await fsm.try_advance(uid, character, "approach")
    assert r.status == fsm.STATUS_OK and r.node.node_id == "s1_talk", r
    r = await fsm.try_advance(uid, character, "end")
    assert r.status in (fsm.STATUS_OK, fsm.STATUS_END) and r.node.node_id == "s1_end", r
    print("[ok] FSM 合法转移按剧情图推进")

    # ---------- 3) 非法转移 ----------
    r_bad = await fsm.try_advance(uid, character, "nonexistent_choice")
    assert r_bad.status == fsm.STATUS_INVALID, r_bad
    # 状态不变（仍在 s1_end）
    st_after = await fsm.get_state(uid, character)
    assert st_after.node_id == "s1_end", st_after
    print("[ok] FSM 非法转移返回 invalid 且不改变状态")

    # ---------- 4) 阶段边界未解锁 -> needs_unlock，不推进 ----------
    r_unlock = await fsm.try_advance(uid, character, "go_stage2")
    assert r_unlock.status == fsm.STATUS_NEEDS_UNLOCK, r_unlock
    assert r_unlock.unlock_stage == 2, r_unlock
    st_still = await fsm.get_state(uid, character)
    assert st_still.stage == 1 and st_still.node_id == "s1_end", st_still
    assert not await store.is_stage_unlocked(uid, character, 2)
    print("[ok] FSM 阶段边界未解锁返回 needs_unlock，不推进、不写解锁")

    # ---------- 5) 解锁定价集中配置 ----------
    assert payment.stage_price_usdt(1) == 2.0
    assert payment.stage_price_usdt(2) == 3.0
    assert payment.stage_price_usdt(3) == 5.0
    try:
        payment.stage_price_usdt(99)
        raise AssertionError("未知阶段应抛 ValueError")
    except ValueError:
        pass
    print("[ok] 三阶段 USDT 定价集中可配（2 / 3 / 5），未知阶段拒绝")

    # ---------- 6) 抽象支付完整流程 create -> confirm -> 解锁 + 推进 ----------
    # 用全新 Mock provider（隔离 _paid 状态），注入为当前 provider
    mock = payment.MockUSDTProvider()
    payment.set_provider(mock)

    res = await payment.create_unlock_charge(uid, character, 2)
    assert res.already_unlocked is False and res.charge is not None
    charge_id = res.charge.charge_id
    assert res.charge.usdt_amount == 3.0, res.charge
    # 支付记录已写 pending
    rec = await store.get_charge(charge_id)
    assert rec is not None and rec.status == store.CHARGE_PENDING and rec.stage == 2

    # 未支付时确认 -> ok=False，不解锁
    cr_unpaid = await payment.confirm_unlock(charge_id, provider=mock)
    assert cr_unpaid.ok is False, cr_unpaid
    assert not await store.is_stage_unlocked(uid, character, 2)

    # 标记已支付后确认 -> 解锁 + FSM 推进到阶段2入口
    mock.mark_paid(charge_id)
    cr = await payment.confirm_unlock(charge_id, provider=mock)
    assert cr.ok is True and cr.unlocked_now is True, cr
    assert cr.state is not None and cr.state.stage == 2 and cr.state.node_id == "s2_intro", cr.state
    assert await store.is_stage_unlocked(uid, character, 2)
    print("[ok] 抽象支付流程：create→confirm→写解锁记录→推进 FSM 到阶段2")

    # ---------- 7) 解锁后阶段边界可推进 ----------
    # 当前已在 s2_intro，走到 s2_end，再尝试进入阶段3（未解锁）
    r2 = await fsm.try_advance(uid, character, "look")
    assert r2.node.node_id == "s2_end", r2
    r_need3 = await fsm.try_advance(uid, character, "go_stage3")
    assert r_need3.status == fsm.STATUS_NEEDS_UNLOCK and r_need3.unlock_stage == 3
    print("[ok] 解锁后进入阶段2、走到阶段3边界再次触发 needs_unlock")

    # ---------- 8) 解锁幂等：不重复收费 / 不重复解锁 ----------
    # 已解锁阶段2，再创建订单应 already_unlocked，不新建 charge
    res_dup = await payment.create_unlock_charge(uid, character, 2)
    assert res_dup.already_unlocked is True and res_dup.charge is None
    # 对阶段2 charge 重复 confirm：unlocked_now=False（不重复新增）
    cr_again = await payment.confirm_unlock(charge_id, provider=mock)
    assert cr_again.ok is True and cr_again.unlocked_now is False, cr_again
    # 解锁记录仍只有阶段2一条
    unlocked = await store.list_unlocked_stages(uid, character)
    assert unlocked == [2], f"解锁阶段应只有 [2]，得到 {unlocked}"
    print("[ok] 解锁幂等：已解锁阶段不重复收费、重复确认不重复解锁")

    # ---------- 9) 支付记录状态流转 ----------
    rec2 = await store.get_charge(charge_id)
    assert rec2.status == store.CHARGE_CONFIRMED and rec2.confirmed_at is not None
    print("[ok] 支付记录 pending -> confirmed，confirmed_at 落库")

    # ---------- 10) provider 可插拔 ----------
    assert isinstance(payment.MockUSDTProvider(), payment.PaymentProvider)

    class AutoPaidProvider(payment.PaymentProvider):
        """自定义 provider：创建即视为已支付（演示真实渠道可替换）。"""

        name = "autopaid"

        async def create_charge(self, *, user_id, character, stage, usdt_amount):
            return payment.ChargeInfo(
                charge_id=f"auto_{stage}_{user_id}",
                usdt_amount=usdt_amount,
                pay_address="AUTO_ADDR",
                pay_info="auto",
            )

        async def confirm_charge(self, charge_id: str) -> bool:
            return True

    auto = AutoPaidProvider()
    payment.set_provider(auto)
    uid3 = 90003
    await fsm.start_story(uid3, character)
    res3 = await payment.create_unlock_charge(uid3, character, 1, provider=auto)
    assert res3.already_unlocked is False and res3.charge is not None
    cr3 = await payment.confirm_unlock(res3.charge.charge_id, provider=auto)
    assert cr3.ok is True and cr3.unlocked_now is True and cr3.state.stage == 1
    # 记录里 provider 名是 autopaid
    rec3 = await store.get_charge(res3.charge.charge_id)
    assert rec3.provider == "autopaid", rec3.provider
    print("[ok] 支付层可插拔：自定义 provider 跑通 create→confirm→解锁")

    # default get_provider 仍可用（mock 回落）
    payment.set_provider(payment.MockUSDTProvider())
    assert isinstance(payment.get_provider(), payment.MockUSDTProvider)

    await store.close_store()
    print("\nALL RSTORY SMOKE TESTS PASSED")


def test_rstory_smoke():
    """pytest 入口（make test-verbose 走 pytest 时调用）。"""
    asyncio.run(main())


if __name__ == "__main__":
    asyncio.run(main())
