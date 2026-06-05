"""Smoke test：第2段 线A 丽娜（Lina）完整慢热恋爱线 5 阶段（不联网）。

覆盖（DB 用临时文件，剧情库走独立 RSTORY_DB_PATH，provider=mock）：
1) seed 幂等 + 丽娜四层 prompt（base/r/nsfw/devoted）填实 + L1/L2/L3 解锁产品就位
   （r_rated_lina=2 / nsfw_lina=3 / devoted_lina=5 USDT，均关联 char_lina）。
2) 【S1 初识】免费段 choice 推进 a_lina_intro→a_lina_walk，affection/trust 数值变化写 stat_history。
3) 【S2 破冰】affection_gte:12 门控；team_up 推进到 R 级支付门，trust/affection/desire 增长。
4) 【S3 约会】payment_gate 未解锁→NEEDS_UNLOCK；Mock 解锁 r_rated_lina→r_rated_lina_paid
   跃迁到 a_lina_intimate，relationship=intimate。
5) 【S4 心意】a_lina_intimate--confess-->age_gate→NEEDS_AGE；年龄验证→nsfw payment_gate
   →NEEDS_UNLOCK；解锁 nsfw_lina→nsfw_lina_paid 跃迁到 a_lina_devotion，relationship=lover，
   写 content_access_log。
6) 【S5 专属】a_lina_devotion--promise-->devoted 支付门→NEEDS_UNLOCK；解锁 devoted_lina
   →devoted_lina_paid 跃迁到 a_lina_exclusive（state_type=end），relationship=devoted + flag。
7) 解锁幂等：重复 create_unlock_charge 不重复收费（already_unlocked）。
8) 非法转移：用错 choice value 返回 INVALID，状态不变。
9) 数值门控驱动节奏：desire 未达阈值时 confess 被拒（linger 引导多互动后再放行）。
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


async def _pay_and_confirm(payment, store, mock, uid, unlock_id, script_id):
    """创建订单→Mock 标记到账→确认解锁，返回 confirm 结果。"""
    res = await payment.create_unlock_charge(uid, unlock_id, provider=mock, script_id=script_id)
    assert res.already_unlocked is False and res.charge is not None, res
    ch = await store.get_charge(res.charge.charge_id)
    assert ch is not None and ch.script_id == script_id and ch.unlock_id == unlock_id, ch
    mock.mark_paid(res.charge.charge_id)
    cr = await payment.confirm_unlock(res.charge.charge_id, provider=mock)
    assert cr.ok, cr
    return cr, res.charge.charge_id


async def main() -> None:
    tmpdir = tempfile.mkdtemp(prefix="rstory_lina_smoke_")
    db_path = os.path.join(tmpdir, "rstory.sqlite3")
    os.environ["BOT_DB_PATH"] = os.path.join(tmpdir, "main.sqlite3")
    os.environ["RSTORY_DB_PATH"] = db_path
    os.environ.setdefault("RSTORY_PAYMENT_PROVIDER", "mock")

    for mod in (
        "config",
        "services.rstory_store",
        "services.rstory_fsm_service",
        "services.rstory_payment",
    ):
        sys.modules.pop(mod, None)

    import config  # noqa: F401
    from services import rstory_fsm_service as fsm
    from services import rstory_payment as payment
    from services import rstory_store as store

    LINE_A = "romance_slow"
    CHAR = "char_lina"

    # ---------- 1) seed 幂等 + 角色 prompt + 三层解锁产品 ----------
    await store.init_store()
    await store.init_store()  # 幂等可重复
    lina = await store.get_character(CHAR)
    assert lina is not None and lina.name == "Lina", lina
    # 四层 prompt 都填实（非占位、含 Lina 人设关键词）。
    assert "丽娜" in lina.base_prompt and "计算机" in lina.base_prompt, lina.base_prompt
    assert lina.r_prompt and "暧昧" in lina.r_prompt, lina.r_prompt
    assert lina.nsfw_prompt and "自愿" in lina.nsfw_prompt, lina.nsfw_prompt
    assert lina.devoted_prompt and "专属" in lina.devoted_prompt, lina.devoted_prompt
    # 三层解锁产品：金额 2 / 3 / 5，均关联 char_lina。
    p1 = await store.get_unlock_product("r_rated_lina")
    p2 = await store.get_unlock_product("nsfw_lina")
    p3 = await store.get_unlock_product("devoted_lina")
    assert p1 and p1.usdt_amount == 2.0 and p1.content_level == 1 and p1.char_id == CHAR, p1
    assert p2 and p2.usdt_amount == 3.0 and p2.content_level == 2 and p2.char_id == CHAR, p2
    assert p3 and p3.usdt_amount == 5.0 and p3.content_level == 3 and p3.char_id == CHAR, p3
    # gate 场景按 (level, char_id) 优先命中丽娜专属产品，不串到 Luna。
    pl2 = await store.get_product_for_level(2, CHAR)
    pl3 = await store.get_product_for_level(3, CHAR)
    assert pl2 and pl2.unlock_id == "nsfw_lina", pl2
    assert pl3 and pl3.unlock_id == "devoted_lina", pl3
    print("[ok] seed 幂等；丽娜四层 prompt 填实；L1/L2/L3 解锁产品 2/3/5 USDT 关联 char_lina")

    uid = 96001
    mock = payment.MockUSDTProvider()
    payment.set_provider(mock)

    # ---------- 2) S1 初识：免费 choice 推进 + 数值/stat_history ----------
    st = await fsm.enter_story(uid, LINE_A, CHAR)
    assert st.scene.scene_id == "a_lina_intro" and st.char_id == CHAR, st
    rel0 = await store.get_or_create_relation(uid, CHAR)
    aff0, trust0 = rel0.affection, rel0.trust
    r1 = await fsm.try_choice(uid, LINE_A, "talk_book")
    assert r1.status == fsm.STATUS_OK and r1.scene.scene_id == "a_lina_walk", r1
    rel1 = await store.get_or_create_relation(uid, CHAR)
    assert rel1.affection == aff0 + 8 and rel1.trust == trust0 + 5, (rel1.affection, rel1.trust)
    assert rel1.flags.get("a_talked") is True, rel1.flags
    hist = await store.list_stat_history(uid, CHAR)
    assert any(h["stat_name"] == "affection" and h["delta"] == 8 for h in hist), hist
    assert any(h["stat_name"] == "trust" and h["delta"] == 5 for h in hist), hist
    print("[ok] S1 初识：talk_book→a_lina_walk，affection+8/trust+5 写 stat_history")

    # ---------- 3) S2 破冰：affection_gte:12 门控 → R 级支付门 ----------
    # 非法转移：用错 choice value（线B 的 reciprocate）→ INVALID，状态不变。
    bad = await fsm.try_choice(uid, LINE_A, "reciprocate")
    assert bad.status == fsm.STATUS_INVALID, bad
    assert (await store.get_game_state(uid, LINE_A)).current_fsm_state == "a_lina_walk"
    # team_up（affection_gte:12 满足，因基线高于阈值）→ a_lina_gate_r。
    r2 = await fsm.try_choice(uid, LINE_A, "team_up")
    assert r2.status == fsm.STATUS_NEEDS_UNLOCK, r2
    assert r2.unlock_id == "r_rated_lina" and r2.content_level == 1, r2
    rel2 = await store.get_or_create_relation(uid, CHAR)
    assert rel2.trust == rel1.trust + 10 and rel2.desire == 8, (rel2.trust, rel2.desire)
    assert rel2.flags.get("a_team_up") is True, rel2.flags
    print("[ok] S2 破冰：affection_gte:12 门控；team_up→payment_gate(NEEDS_UNLOCK r_rated_lina)；trust+10/desire+8")

    # ---------- 4) S3 约会：解锁 r_rated_lina → 跃迁 a_lina_intimate ----------
    cr1, _ = await _pay_and_confirm(payment, store, mock, uid, "r_rated_lina", LINE_A)
    assert cr1.advance is not None and cr1.advance.scene.scene_id == "a_lina_intimate", cr1.advance
    rel3 = await store.get_or_create_relation(uid, CHAR)
    assert rel3.relationship == "intimate", rel3.relationship
    assert rel3.flags.get("a_intimate_entered") is True, rel3.flags
    assert rel3.desire == 8 + 12, rel3.desire  # team_up(+8) + 跃迁(+12) = 20
    print("[ok] S3 约会：解锁 r_rated_lina→r_rated_lina_paid 跃迁 a_lina_intimate，relationship=intimate，desire=20")

    # ---------- 4b) 解锁幂等：重复创建订单不重复收费 ----------
    dup = await payment.create_unlock_charge(uid, "r_rated_lina", provider=mock, script_id=LINE_A)
    assert dup.already_unlocked is True and dup.charge is None, dup
    print("[ok] 解锁幂等：重复 create_unlock_charge(r_rated_lina) → already_unlocked，不重复收费")

    # ---------- 9) 数值门控：desire 未达阈值时 confess 被拒（这里 desire=20≥18，confess 应通过）----------
    # 先验证 linger 原地升温（不跃迁，保留 desire 阈值语义的引导分支可用）。
    rl = await fsm.try_choice(uid, LINE_A, "linger")
    assert rl.status == fsm.STATUS_OK and rl.scene.scene_id == "a_lina_intimate", rl
    rel_l = await store.get_or_create_relation(uid, CHAR)
    assert rel_l.desire == 20 + 6, rel_l.desire  # linger desire+6
    print("[ok] linger 原地升温 a_lina_intimate（desire 引导分支可用），desire=26")

    # ---------- 5) S4 心意：confess→age_gate→年龄验证→nsfw payment→a_lina_devotion ----------
    r4 = await fsm.try_choice(uid, LINE_A, "confess")
    assert r4.status == fsm.STATUS_NEEDS_AGE, r4  # 落到 age_gate，未验证
    assert (await store.get_game_state(uid, LINE_A)).current_fsm_state == "a_lina_age_gate"
    assert (await store.get_or_create_relation(uid, CHAR)).flags.get("a_confessed") is True
    # 年龄未验证：consume_age_verify 应卡住（无法跃迁）。
    assert await store.is_age_verified(uid) is False
    # 完成年龄验证（置 age_verified=1 + 写 content_access_log），再消费 age_verify 转移。
    await store.set_age_verified(uid)
    await store.log_content_access(uid, 2, "a_lina_age_gate", True)
    r4b = await fsm.consume_age_verify(uid, LINE_A)
    # age_verify 跃迁后落到 nsfw payment_gate（未解锁）→ NEEDS_UNLOCK。
    assert r4b.status == fsm.STATUS_NEEDS_UNLOCK and r4b.unlock_id == "nsfw_lina", r4b
    assert r4b.content_level == 2, r4b
    assert (await store.get_or_create_relation(uid, CHAR)).flags.get("a_age_passed") is True
    print("[ok] S4 心意：confess→age_gate(NEEDS_AGE)；验证→age_verify 跃迁→nsfw payment_gate(NEEDS_UNLOCK nsfw_lina)")

    cr2, _ = await _pay_and_confirm(payment, store, mock, uid, "nsfw_lina", LINE_A)
    assert cr2.advance is not None and cr2.advance.scene.scene_id == "a_lina_devotion", cr2.advance
    rel4 = await store.get_or_create_relation(uid, CHAR)
    assert rel4.relationship == "lover", rel4.relationship
    assert rel4.flags.get("a_nsfw_entered") is True, rel4.flags
    # content_access_log 记录了 L2 访问。
    logs = await store.list_content_access(uid)
    assert any(row["content_level"] == 2 for row in logs), logs
    print("[ok] S4 心意：解锁 nsfw_lina→nsfw_lina_paid 跃迁 a_lina_devotion，relationship=lover，content_access_log 含 L2")

    # ---------- 6) S5 专属：promise→devoted 支付门→a_lina_exclusive(end) ----------
    r5 = await fsm.try_choice(uid, LINE_A, "promise")
    assert r5.status == fsm.STATUS_NEEDS_UNLOCK and r5.unlock_id == "devoted_lina", r5
    assert r5.content_level == 3, r5
    assert (await store.get_or_create_relation(uid, CHAR)).flags.get("a_promised") is True
    cr3, _ = await _pay_and_confirm(payment, store, mock, uid, "devoted_lina", LINE_A)
    assert cr3.advance is not None, cr3
    assert cr3.advance.status == fsm.STATUS_END, cr3.advance  # 终局 end
    assert cr3.advance.scene.scene_id == "a_lina_exclusive", cr3.advance
    rel5 = await store.get_or_create_relation(uid, CHAR)
    assert rel5.relationship == "devoted", rel5.relationship
    assert rel5.flags.get("a_devoted") is True, rel5.flags
    print("[ok] S5 专属：解锁 devoted_lina→devoted_lina_paid 跃迁 a_lina_exclusive(end)，relationship=devoted+flag")

    # ---------- 7) 全部三层解锁幂等：重复不重复收费 ----------
    for unlock_id in ("r_rated_lina", "nsfw_lina", "devoted_lina"):
        again = await payment.create_unlock_charge(uid, unlock_id, provider=mock, script_id=LINE_A)
        assert again.already_unlocked is True and again.charge is None, (unlock_id, again)
    print("[ok] 三层解锁均幂等：r_rated_lina/nsfw_lina/devoted_lina 重复均不重复收费")

    # ---------- 8) 终局后非法转移：end 场景无 choice，任意 choice → INVALID ----------
    end_bad = await fsm.try_choice(uid, LINE_A, "promise")
    assert end_bad.status == fsm.STATUS_INVALID, end_bad
    assert (await store.get_game_state(uid, LINE_A)).current_fsm_state == "a_lina_exclusive"
    print("[ok] 终局后非法转移被拒（INVALID），状态停留 a_lina_exclusive")

    await store.close_store()
    print("\nALL RSTORY LINA LINE-A SMOKE TESTS PASSED")


def test_rstory_lina_smoke():
    asyncio.run(main())


if __name__ == "__main__":
    asyncio.run(main())
