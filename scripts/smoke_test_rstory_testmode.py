"""Smoke test：内测模式开关 RSTORY_TEST_MODE 付费门放行（不联网）。

覆盖（DB 用临时文件，剧情库走独立 RSTORY_DB_PATH，provider=mock）：
1) RSTORY_TEST_MODE=True 时 payment_gate 直接放行：
   - 不创建任何 rstory_charges（不走 create_charge / 收款流程）。
   - 写入一条 source=test_mode 的 user_unlocks 解锁记录。
   - payment 转移正常跃迁（relationship / flag / 数值照常）。
2) test_mode 下 age_gate 也对放行用户跳过：视同已验证年龄、写 content_access_log 审计，继续跃迁。
3) RSTORY_TEST_MODE=False 时同一条 payment_gate 仍走原 create_charge 流程（mock），
   未解锁返回 NEEDS_UNLOCK；解锁来源为 oxapay（非 test_mode）。
4) 清理：DELETE FROM user_unlocks WHERE source='test_mode' 只清掉测试解锁，不误删真实解锁。
"""

from __future__ import annotations

import asyncio
import importlib
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

LINE_A = "romance_slow"
CHAR = "char_lina"

# 服务模块（含模块级连接状态）随每个阶段重载，确保拿到全新 store 连接。
_SERVICE_MODS = (
    "services.rstory_store",
    "services.rstory_fsm_service",
    "services.rstory_payment",
)


def _reload_with_test_mode(value: str):
    """按指定 RSTORY_TEST_MODE 重新加载相关模块，返回 (config, fsm, payment, store)。

    用 importlib.reload(config) 原地刷新 config（所有 `import config` 引用共享同一对象，
    避免 pop+import 产生新对象、旧服务模块仍绑旧 config 的串台问题）。
    """
    os.environ["RSTORY_TEST_MODE"] = value
    import config

    importlib.reload(config)
    for mod in _SERVICE_MODS:
        sys.modules.pop(mod, None)
    from services import rstory_fsm_service as fsm
    from services import rstory_payment as payment
    from services import rstory_store as store

    return config, fsm, payment, store


async def _advance_to_first_payment_gate(fsm, store, uid):
    """把用户推进到第一个 R 级 payment_gate（a_lina_gate_r），返回引擎结果。"""
    await fsm.enter_story(uid, LINE_A, CHAR)
    await fsm.try_choice(uid, LINE_A, "talk_book")
    return await fsm.try_choice(uid, LINE_A, "team_up")


async def main() -> None:
    tmpdir = tempfile.mkdtemp(prefix="rstory_testmode_smoke_")
    db_path = os.path.join(tmpdir, "rstory.sqlite3")
    os.environ["BOT_DB_PATH"] = os.path.join(tmpdir, "main.sqlite3")
    os.environ["RSTORY_DB_PATH"] = db_path
    os.environ.setdefault("RSTORY_PAYMENT_PROVIDER", "mock")

    # =================== A) RSTORY_TEST_MODE=True 放行 ===================
    config, fsm, payment, store = _reload_with_test_mode("true")
    assert config.RSTORY_TEST_MODE is True, config.RSTORY_TEST_MODE
    await store.init_store()
    mock = payment.MockUSDTProvider()
    payment.set_provider(mock)

    uid_t = 97001
    res = await _advance_to_first_payment_gate(fsm, store, uid_t)
    # 内测放行：payment_gate 不返回 NEEDS_UNLOCK，而是直接跃迁到 R 级亲密场景。
    assert res.status == fsm.STATUS_OK, res
    assert res.scene.scene_id == "a_lina_intimate", res.scene.scene_id
    # 写了一条 source=test_mode 的解锁记录。
    assert await store.is_unlocked(uid_t, "r_rated_lina") is True
    unlocks = await store._fetchall(
        "SELECT unlock_id, source FROM user_unlocks WHERE user_id = ?",
        (store._norm_uid(uid_t),),
    )
    assert any(
        r["unlock_id"] == "r_rated_lina" and r["source"] == "test_mode" for r in unlocks
    ), unlocks
    # 没有走收款：未创建任何 rstory_charges 订单。
    charges = await store._fetchall(
        "SELECT charge_id FROM rstory_charges WHERE user_id = ?",
        (str(store._norm_uid(uid_t)),),
    )
    assert charges == [], charges
    # 数值/关系跃升照常（与 lina 线一致：解锁后 relationship=intimate）。
    rel = await store.get_or_create_relation(uid_t, CHAR)
    assert rel.relationship == "intimate", rel.relationship
    assert rel.flags.get("a_intimate_entered") is True, rel.flags
    print("[ok] TEST_MODE=True：payment_gate 直接放行（无 charge），写 source=test_mode unlock，正常跃迁 intimate")

    # ---------- A2) test_mode 下 age_gate 也对放行用户跳过（含审计 + 后续 nsfw payment 放行）----------
    # 升级后语义：RSTORY_TEST_MODE=True 视同已验证年龄，age_gate 直接放行（仍写 content_access_log 审计）。
    # confess 落到 age_gate → 自动放行 → 继续走到 nsfw payment_gate → 又放行，最终落到 a_lina_devotion。
    await fsm.try_choice(uid_t, LINE_A, "linger")  # desire 升到阈值，confess 可推进到 age_gate
    r_age = await fsm.try_choice(uid_t, LINE_A, "confess")
    assert r_age.status == fsm.STATUS_OK, r_age
    assert r_age.scene.scene_id == "a_lina_devotion", r_age.scene.scene_id
    # age_gate 放行视同已验证年龄，并写了 content_access_log 审计痕迹（age_verified=1）。
    assert await store.is_age_verified(uid_t) is True
    access = await store.list_content_access(uid_t)
    assert any(r["age_verified"] for r in access), access
    assert await store.is_unlocked(uid_t, "nsfw_lina") is True
    print("[ok] TEST_MODE=True：age_gate 也放行（视同已验证 + 写 content_access_log 审计），nsfw payment 再放行")

    await store.close_store()

    # =================== B) RSTORY_TEST_MODE=False 仍走原 create_charge ===================
    config, fsm, payment, store = _reload_with_test_mode("false")
    assert config.RSTORY_TEST_MODE is False, config.RSTORY_TEST_MODE
    await store.init_store()
    mock = payment.MockUSDTProvider()
    payment.set_provider(mock)

    uid_f = 97002
    res_f = await _advance_to_first_payment_gate(fsm, store, uid_f)
    # 正常收费：payment_gate 未解锁 → NEEDS_UNLOCK，不自动放行。
    assert res_f.status == fsm.STATUS_NEEDS_UNLOCK, res_f
    assert res_f.unlock_id == "r_rated_lina", res_f
    assert await store.is_unlocked(uid_f, "r_rated_lina") is False
    # 走原 create_charge（mock）流程才解锁，来源为 oxapay（非 test_mode）。
    charge_res = await payment.create_unlock_charge(uid_f, "r_rated_lina", provider=mock, script_id=LINE_A)
    assert charge_res.charge is not None, charge_res
    mock.mark_paid(charge_res.charge.charge_id)
    confirm = await payment.confirm_unlock(charge_res.charge.charge_id, provider=mock)
    assert confirm.ok and confirm.advance is not None, confirm
    assert confirm.advance.scene.scene_id == "a_lina_intimate", confirm.advance
    src_rows = await store._fetchall(
        "SELECT source FROM user_unlocks WHERE user_id = ? AND unlock_id = ?",
        (store._norm_uid(uid_f), "r_rated_lina"),
    )
    assert src_rows and src_rows[0]["source"] != "test_mode", src_rows
    print("[ok] TEST_MODE=False：payment_gate 仍 NEEDS_UNLOCK；走 mock create_charge 解锁，source!=test_mode")

    # =================== C) 清理：只删 source=test_mode ===================
    # 在同一个 False 库里插一条真实解锁 + 一条 test_mode 解锁，验证清理脚本只清测试记录。
    await store.record_unlock(uid_f, "nsfw_lina", source=store.UNLOCK_SOURCE_TEST_MODE)
    assert await store.is_unlocked(uid_f, "nsfw_lina") is True
    await store._execute("DELETE FROM user_unlocks WHERE source = ?", ("test_mode",))
    # test_mode 那条被清掉，真实 oxapay 解锁仍在。
    assert await store.is_unlocked(uid_f, "nsfw_lina") is False
    assert await store.is_unlocked(uid_f, "r_rated_lina") is True
    print("[ok] 清理：DELETE WHERE source='test_mode' 只清测试解锁，真实付费解锁保留")

    await store.close_store()
    print("\nALL RSTORY TEST_MODE SMOKE TESTS PASSED")


def test_rstory_testmode_smoke():
    asyncio.run(main())


if __name__ == "__main__":
    asyncio.run(main())
