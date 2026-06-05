"""Smoke test：R 级互动剧情系统 —— 数据驱动 FSM 引擎（不联网）。

覆盖（DB 用临时文件，剧情库走独立 RSTORY_DB_PATH，provider=mock）：
1) 启动建表 + 种子幂等（executescript 跑两次不报错；种子可读）。
2) 数据驱动 FSM 从 DB 读规则推进：scene_intro --enter--> scene_hall。
3) effect_json 生效：set_flag + *_delta 写入 user_char_relation 并记 stat_history。
4) condition_json 求值：AND / 数值阈值(desire_gte) / flag_set / content_level_unlocked。
5) priority 选择：scene_hall --closer-->，desire>=60 时优先走 gate_r_payment(priority 20)，
   否则走 scene_ai_free(priority 5)。
6) payment_gate -> 创建 OxaPay/Mock 订单 -> 解锁 -> payment 跃迁进入 scene_r_soft。
7) age_gate -> 年龄验证 -> age_verify 跃迁。
8) 解锁幂等：已解锁产品不重复收费；重复结算不重复加 user_unlocks。
9) 非法转移：不匹配的 choice 返回 invalid，状态不变。
10) auto 跃迁：满足条件自动连跳（scene_ai_free -> scene_good_end）。
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

    script_id = fsm.DEFAULT_SCRIPT_ID

    # ---------- 1) 建表 + 种子幂等 ----------
    await store.init_store()
    await store.init_store()  # 第二次幂等
    script = await store.get_script(script_id)
    assert script is not None and script.entry_state == "scene_intro", script
    luna = await store.get_character("char_luna")
    assert luna is not None and luna.name == "Luna" and luna.r_prompt, luna
    prod_r = await store.get_unlock_product("r_rated")
    assert prod_r is not None and prod_r.usdt_amount == 2.0, prod_r
    assert (await store.get_unlock_product("nsfw_char_luna")).usdt_amount == 3.0
    assert (await store.get_unlock_product("devoted_char_luna")).usdt_amount == 5.0
    print("[ok] 启动建表 + 种子幂等；USDT 价 2/3/5 来自 unlock_products")

    # ---------- 2) 数据驱动推进 + 3) effect + stat_history ----------
    uid = 90002
    state = await fsm.start_story(uid, script_id)
    assert state.scene.scene_id == "scene_intro", state.scene
    r = await fsm.try_choice(uid, script_id, "enter")
    assert r.status == fsm.STATUS_OK and r.scene.scene_id == "scene_hall", r
    rel = await store.get_or_create_relation(uid, "char_luna")
    assert rel.flags.get("entered_mansion") is True, rel.flags
    assert rel.trust == 35, f"trust 应 30+5=35，得到 {rel.trust}"  # default 30 + delta 5
    hist = await store.list_stat_history(uid, "char_luna")
    assert any(h["stat_name"] == "trust" and h["delta"] == 5 for h in hist), list(hist)
    print("[ok] 数据驱动 enter 推进 + effect(set_flag/trust_delta) + stat_history 落库")

    # ---------- 5) priority：desire 不足时 closer 走低优先级 scene_ai_free ----------
    # 当前在 scene_hall，desire=0 -> gate_r_payment(需 desire>=60) 不满足，落 scene_ai_free(priority 5)
    r2 = await fsm.try_choice(uid, script_id, "closer")
    assert r2.scene.scene_id == "scene_ai_free", r2
    rel2 = await store.get_or_create_relation(uid, "char_luna")
    assert rel2.flags.get("closer_attempt") is True
    assert rel2.desire == 15, f"desire 应 0+15=15，得到 {rel2.desire}"
    print("[ok] priority：desire 不足时 closer 走低优先级 scene_ai_free 转移")

    # ---------- 9) 非法转移：状态不变 ----------
    bad = await fsm.try_choice(uid, script_id, "nonexistent")
    assert bad.status == fsm.STATUS_INVALID, bad
    st_now = await fsm.get_state(uid, script_id)
    assert st_now.scene.scene_id == "scene_ai_free", st_now.scene
    print("[ok] 非法转移返回 invalid，状态不变")

    # ---------- 5b) priority：desire 充足时 closer 优先走 gate_r_payment ----------
    uid2 = 90010
    await fsm.start_story(uid2, script_id)
    await fsm.try_choice(uid2, script_id, "enter")  # -> scene_hall
    # 直接拉高 desire 到 60+，验证优先级匹配 + condition 求值
    await store.apply_relation_changes(uid2, "char_luna", deltas={"desire": 65}, reason="test_setup")
    rrich = await fsm.try_choice(uid2, script_id, "closer")
    # 落到 payment_gate(gate_r_payment) 未解锁 -> NEEDS_UNLOCK
    assert rrich.status == fsm.STATUS_NEEDS_UNLOCK, rrich
    assert rrich.unlock_id == "r_rated", rrich
    assert rrich.content_level == 1, rrich
    print("[ok] priority + condition：desire>=60 时 closer 优先进 gate_r_payment（需解锁 r_rated）")

    # ---------- 6) payment_gate -> 创建订单 -> 解锁 -> payment 跃迁 ----------
    mock = payment.MockUSDTProvider()
    payment.set_provider(mock)
    res = await payment.create_unlock_charge(uid2, "r_rated", provider=mock)
    assert res.already_unlocked is False and res.charge is not None, res
    charge_id = res.charge.charge_id
    assert res.charge.usdt_amount == 2.0, res.charge
    rec = await store.get_charge(charge_id)
    assert rec is not None and rec.status == store.CHARGE_PENDING and rec.unlock_id == "r_rated"

    # 未支付确认 -> ok=False，不解锁
    cr_unpaid = await payment.confirm_unlock(charge_id, provider=mock)
    assert cr_unpaid.ok is False and not await store.is_unlocked(uid2, "r_rated")

    # 标记已支付 -> 解锁 + 消费 payment 跃迁到 scene_r_soft
    mock.mark_paid(charge_id)
    cr = await payment.confirm_unlock(charge_id, provider=mock)
    assert cr.ok is True and cr.unlocked_now is True, cr
    assert await store.is_unlocked(uid2, "r_rated")
    assert await store.is_level_unlocked(uid2, 1), "content_level 1 应已解锁"
    assert cr.advance is not None and cr.advance.scene.scene_id == "scene_r_soft", cr.advance
    rel_r = await store.get_or_create_relation(uid2, "char_luna")
    assert rel_r.relationship == "intimate", rel_r.relationship  # payment 跃迁 effect
    assert rel_r.flags.get("r_scene_entered") is True
    print("[ok] payment_gate→订单→解锁 r_rated→payment 跃迁进入 scene_r_soft（relationship=intimate）")

    # ---------- 4) condition content_level_unlocked 求值（已解锁 level1 才允许 payment 跃迁）----------
    # 上一步 payment 转移 condition 含 content_level_unlocked:1 + desire_gte:60，已满足才跃迁成功，已隐含验证。
    # 显式再验 content_level_unlocked 求值器：
    rel_check = await store.get_or_create_relation(uid2, "char_luna")
    assert await fsm.evaluate_condition({"AND": [{"content_level_unlocked": 1}]}, uid2, rel_check) is True
    assert await fsm.evaluate_condition({"AND": [{"content_level_unlocked": 2}]}, uid2, rel_check) is False
    assert await fsm.evaluate_condition({"AND": [{"desire_gte": 60}]}, uid2, rel_check) is True
    assert await fsm.evaluate_condition({"flag_set": "r_scene_entered"}, uid2, rel_check) is True
    assert await fsm.evaluate_condition({"flag_set": "no_such_flag"}, uid2, rel_check) is False
    print("[ok] condition_json 求值：AND / 数值阈值 / flag_set / content_level_unlocked")

    # ---------- 7) auto 跃迁 + age_gate -> 验证 -> 跃迁 ----------
    # scene_r_soft 的 auto 跃迁需要 desire>=80 且 flag r_scene_entered。当前 desire=65+10(payment)=75。
    rel_now = await store.get_or_create_relation(uid2, "char_luna")
    assert rel_now.desire == 75, f"desire 应 65+10=75，得到 {rel_now.desire}"
    # 推一点 desire 触发 auto: scene_r_soft -> gate_age_verify
    await store.apply_relation_changes(uid2, "char_luna", deltas={"desire": 10}, reason="test_push")
    # 重新进入触发 auto：用一次 start_story（含 _auto_advance）或直接调内部。用 consume + get。
    # 简便：重新走 start_story 不会回退状态（已有 game_state），只做 auto 连跳。
    st_auto = await fsm.start_story(uid2, script_id)
    assert st_auto.scene.scene_id == "gate_age_verify", f"应 auto 跳到 gate_age_verify，得到 {st_auto.scene.scene_id}"
    # 此时 age 未验证：通过 get_state 看 state_type
    assert st_auto.scene.state_type == "age_gate"
    assert not await store.is_age_verified(uid2)

    # 年龄验证 -> 消费 age_verify 跃迁 -> 落到 gate_nsfw_payment(payment_gate, 未解锁 -> NEEDS_UNLOCK)
    await store.set_age_verified(uid2)
    adv_age = await fsm.consume_age_verify(uid2, script_id)
    assert adv_age.status == fsm.STATUS_NEEDS_UNLOCK and adv_age.unlock_id == "nsfw_char_luna", adv_age
    assert adv_age.content_level == 2, adv_age
    # content_access_log 有年龄验证记录
    logs = await store.list_content_access(uid2)
    assert any(row["age_verified"] == 1 for row in logs), list(logs)
    print("[ok] auto 跃迁 + age_gate→年龄验证→age_verify 跃迁到 NSFW 支付门")

    # 解锁 nsfw -> payment 跃迁 scene_nsfw（需 age_gate_passed flag，由 age_verify effect 设过）
    res_n = await payment.create_unlock_charge(uid2, "nsfw_char_luna", provider=mock)
    assert res_n.charge.usdt_amount == 3.0
    mock.mark_paid(res_n.charge.charge_id)
    cr_n = await payment.confirm_unlock(res_n.charge.charge_id, provider=mock)
    assert cr_n.ok and cr_n.advance.scene.scene_id == "scene_nsfw", cr_n.advance
    rel_n = await store.get_or_create_relation(uid2, "char_luna")
    assert rel_n.relationship == "lover", rel_n.relationship
    print("[ok] 解锁 nsfw_char_luna→payment 跃迁 scene_nsfw（relationship=lover，需 age_gate_passed）")

    # ---------- 8) 解锁幂等 ----------
    res_dup = await payment.create_unlock_charge(uid2, "r_rated", provider=mock)
    assert res_dup.already_unlocked is True and res_dup.charge is None
    again = await payment.settle_paid_charge(await store.get_charge(charge_id))
    assert again.ok and again.unlocked_now is False, again
    unlocked = await store.list_unlocked(uid2)
    assert sorted(unlocked) == ["nsfw_char_luna", "r_rated"], unlocked
    print("[ok] 解锁幂等：已解锁不重复收费、重复结算不重复加 user_unlocks")

    # ---------- 10) auto 跃迁到 good_end（独立用户走 talk 线）----------
    uid3 = 90020
    await fsm.start_story(uid3, script_id)
    await fsm.try_choice(uid3, script_id, "enter")  # scene_hall
    # talk: affection+8(50->58), trust+10 -> scene_ai_free；good_end 需 affection>=80 且 honest_talk
    rt = await fsm.try_choice(uid3, script_id, "talk")
    assert rt.scene.scene_id == "scene_ai_free", rt
    # 拉高 affection 到 80+ 后重新触发 auto
    await store.apply_relation_changes(uid3, "char_luna", deltas={"affection": 30}, reason="test")
    st_end = await fsm.start_story(uid3, script_id)
    assert st_end.scene.scene_id == "scene_good_end" and st_end.scene.state_type == "end", st_end.scene
    print("[ok] auto 跃迁：affection>=80 且 honest_talk -> scene_good_end（end）")

    await store.close_store()
    print("\nALL RSTORY SMOKE TESTS PASSED")


def test_rstory_smoke():
    asyncio.run(main())


if __name__ == "__main__":
    asyncio.run(main())
