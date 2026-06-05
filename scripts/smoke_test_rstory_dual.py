"""Smoke test：第1段 双剧情线骨架 + 先选角色入口（不联网）。

覆盖（DB 用临时文件，剧情库走独立 RSTORY_DB_PATH，provider=mock）：
1) seed 幂等 + 两条线 romance_slow / bold_pursuit 与角色 Lina/Izzy 入座。
2) 先选角色入口：list_script_characters 聚合各线角色；list_scripts 列两条线。
3) 进入 (角色, 线)：enter_story 写 user_game_state 到该角色 entry scene。
4) 双线进度隔离：同一用户在 A、B 两线各自独立 current_fsm_state / current_char_id，
   推进一条线不影响另一条线的进度。
5) choice 推进：线A talk_book、线B reciprocate。
6) payment_gate → Mock 解锁 r_rated_lina → payment 跃迁（两条线各自跃迁到各自亲密场景）。
7) 解锁是账号级共享（解锁 L1 后，另一条线遇到同一 L1 门直接放行，不重复收费），
   但剧本进度仍各自独立（结算按订单 script_id 消费对应线的 payment 跃迁）。
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


async def main() -> None:
    tmpdir = tempfile.mkdtemp(prefix="rstory_dual_smoke_")
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
    LINE_B = "bold_pursuit"

    # ---------- 1) seed 幂等 + 两条线/角色入座 ----------
    await store.init_store()
    await store.init_store()  # 幂等
    sa = await store.get_script(LINE_A)
    sb = await store.get_script(LINE_B)
    assert sa is not None and sa.entry_state == "a_lina_intro", sa
    assert sb is not None and sb.entry_state == "b_lina_intro", sb
    lina = await store.get_character("char_lina")
    izzy = await store.get_character("char_izzy")
    assert lina is not None and lina.name == "Lina" and lina.r_prompt, lina
    assert izzy is not None and izzy.name == "Izzy", izzy
    # L1 角色专属解锁产品（payment_gate 优先按 char_id 命中）。
    prod = await store.get_unlock_product("r_rated_lina")
    assert prod is not None and prod.usdt_amount == 2.0 and prod.content_level == 1, prod
    print("[ok] seed 幂等；两条线 romance_slow/bold_pursuit + 角色 Lina/Izzy + L1 产品就位")

    # ---------- 2) 先选角色入口：聚合角色 + 列剧情线 ----------
    chars_a = await store.list_script_characters(LINE_A)
    chars_b = await store.list_script_characters(LINE_B)
    ids_a = {c.char_id for c in chars_a}
    ids_b = {c.char_id for c in chars_b}
    assert {"char_lina", "char_izzy"} <= ids_a, ids_a
    assert {"char_lina", "char_izzy"} <= ids_b, ids_b
    scripts = {s.script_id for s in await store.list_scripts(active_only=True)}
    assert {LINE_A, LINE_B} <= scripts, scripts
    print("[ok] 先选角色入口：两线都含 Lina/Izzy；list_scripts 列出 A/B 两条线")

    # ---------- 3) 进入 (Lina, 线A) ----------
    uid = 95001
    st_a = await fsm.enter_story(uid, LINE_A, "char_lina")
    assert st_a.scene.scene_id == "a_lina_intro", st_a.scene
    assert st_a.char_id == "char_lina", st_a.char_id
    gs_a = await store.get_game_state(uid, LINE_A)
    assert gs_a.current_fsm_state == "a_lina_intro" and gs_a.current_char_id == "char_lina"
    print("[ok] 进入 (Lina, 线A) → entry scene a_lina_intro，写 user_game_state")

    # ---------- 3b) 同一用户进入 (Lina, 线B) ----------
    st_b = await fsm.enter_story(uid, LINE_B, "char_lina")
    assert st_b.scene.scene_id == "b_lina_intro", st_b.scene
    gs_b = await store.get_game_state(uid, LINE_B)
    assert gs_b.current_fsm_state == "b_lina_intro", gs_b
    # 线A 进度未被线B 覆盖。
    gs_a2 = await store.get_game_state(uid, LINE_A)
    assert gs_a2.current_fsm_state == "a_lina_intro", gs_a2
    print("[ok] 双线进度隔离：同用户进入线B 不影响线A 的 (user_id, script_id) 行")

    # ---------- 5) choice 推进（两线各自推进，互不串线）----------
    ra = await fsm.try_choice(uid, LINE_A, "talk_book")
    assert ra.status == fsm.STATUS_OK and ra.scene.scene_id == "a_lina_walk", ra
    # 线B 仍停在 entry，未受线A choice 影响。
    assert (await store.get_game_state(uid, LINE_B)).current_fsm_state == "b_lina_intro"
    rb = await fsm.try_choice(uid, LINE_B, "reciprocate")
    assert rb.status == fsm.STATUS_OK and rb.scene.scene_id == "b_lina_closer", rb
    # 线A 仍停在 a_lina_walk。
    assert (await store.get_game_state(uid, LINE_A)).current_fsm_state == "a_lina_walk"
    print("[ok] choice 推进：线A talk_book→a_lina_walk、线B reciprocate→b_lina_closer，互不串线")

    # 非法 choice（用错线的 value）→ INVALID，状态不变。
    bad = await fsm.try_choice(uid, LINE_A, "reciprocate")
    assert bad.status == fsm.STATUS_INVALID, bad
    assert (await store.get_game_state(uid, LINE_A)).current_fsm_state == "a_lina_walk"
    print("[ok] 跨线 choice value 在错误线返回 invalid，状态不变")

    # ---------- 6) 线A：推进到 payment_gate → Mock 解锁 → payment 跃迁 ----------
    mock = payment.MockUSDTProvider()
    payment.set_provider(mock)
    # a_lina_walk --hold_hands--> a_lina_gate_r(payment_gate, L1, 未解锁) → NEEDS_UNLOCK
    ra_gate = await fsm.try_choice(uid, LINE_A, "hold_hands")
    assert ra_gate.status == fsm.STATUS_NEEDS_UNLOCK, ra_gate
    assert ra_gate.unlock_id == "r_rated_lina" and ra_gate.content_level == 1, ra_gate
    # 创建订单（携带 script_id=线A）→ Mock 标记已付 → 确认 → 跃迁到 a_lina_intimate
    res_a = await payment.create_unlock_charge(uid, "r_rated_lina", provider=mock, script_id=LINE_A)
    assert res_a.already_unlocked is False and res_a.charge is not None, res_a
    ch_a = await store.get_charge(res_a.charge.charge_id)
    assert ch_a is not None and ch_a.script_id == LINE_A, ch_a
    mock.mark_paid(res_a.charge.charge_id)
    cr_a = await payment.confirm_unlock(res_a.charge.charge_id, provider=mock)
    assert cr_a.ok and cr_a.advance is not None, cr_a
    assert cr_a.advance.scene.scene_id == "a_lina_intimate", cr_a.advance
    rel_a = await store.get_or_create_relation(uid, "char_lina")
    assert rel_a.relationship == "intimate" and rel_a.flags.get("a_intimate_entered") is True
    print("[ok] 线A payment_gate→Mock 解锁 r_rated_lina→payment 跃迁 a_lina_intimate")

    # 线B 进度仍停在 b_lina_closer（线A 的支付结算没串到线B）。
    assert (await store.get_game_state(uid, LINE_B)).current_fsm_state == "b_lina_closer"
    print("[ok] 线A 支付结算未串线B：线B 仍停在 b_lina_closer")

    # ---------- 7) 线B：同一 L1 门，账号级已解锁 → 直接放行，但按线B 跃迁 ----------
    rb_gate = await fsm.try_choice(uid, LINE_B, "go_closer")
    # r_rated_lina 已账号级解锁 → _gate_or_scene 直接消费线B 的 payment 跃迁。
    assert rb_gate.status == fsm.STATUS_OK, rb_gate
    assert rb_gate.scene.scene_id == "b_lina_passion", rb_gate
    rel_b = await store.get_or_create_relation(uid, "char_lina")
    assert rel_b.flags.get("b_passion_entered") is True, rel_b.flags
    # 重复创建订单走幂等：already_unlocked。
    res_dup = await payment.create_unlock_charge(uid, "r_rated_lina", provider=mock, script_id=LINE_B)
    assert res_dup.already_unlocked is True and res_dup.charge is None
    print("[ok] 线B 同一 L1 门：账号级已解锁→直接放行跃迁 b_lina_passion；重复不收费")

    # 线A 最终态未被线B 推进覆盖。
    assert (await store.get_game_state(uid, LINE_A)).current_fsm_state == "a_lina_intimate"
    print("[ok] 全流程后线A 仍在 a_lina_intimate，双线终态各自独立")

    # ---------- 旧库迁移：ALTER 补 script_id 列幂等（已隐含在 init_store 两次）----------
    assert ch_a.script_id == LINE_A  # 新列可读写

    await store.close_store()
    print("\nALL RSTORY DUAL-LINE SMOKE TESTS PASSED")


def test_rstory_dual_smoke():
    asyncio.run(main())


if __name__ == "__main__":
    asyncio.run(main())
