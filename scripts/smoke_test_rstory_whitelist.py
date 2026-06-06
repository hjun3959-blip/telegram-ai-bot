"""Smoke test：内测白名单按 Telegram 用户 ID 放行（付费门 + 年龄门，不联网）。

升级自 RSTORY_TEST_MODE 全局开关：放行条件改为 OR——
  RSTORY_TEST_MODE=True（全员）或 user_id in RSTORY_TEST_WHITELIST（仅该用户）。

覆盖（DB 用临时文件，剧情库走独立 RSTORY_DB_PATH，provider=mock）：
A) RSTORY_TEST_MODE=False 且白名单含 user_id：
   - payment_gate 直接放行（无 charge），写 source=test_mode unlock，正常跃迁。
   - age_gate 也放行：视同已验证年龄、写 content_access_log 审计，继续跃迁到 nsfw 场景。
   - 数值/relationship 跃升照常（relationship=intimate）。
B) 白名单之外的 user_id（同一 False 库）：payment_gate 仍 NEEDS_UNLOCK，age 仍需验证；
   走 mock create_charge 解锁，source!=test_mode。
C) RSTORY_TEST_MODE=True 时全员放行（即使不在白名单），兼容原行为。
D) config.rstory_test_bypass 判定函数：白名单命中 reason=whitelist；全局命中 reason=global；
   默认 7256055877 在默认白名单内。
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

_SERVICE_MODS = (
    "services.rstory_store",
    "services.rstory_fsm_service",
    "services.rstory_payment",
)


def _reload(test_mode: str, whitelist: str):
    """按指定 RSTORY_TEST_MODE / RSTORY_TEST_WHITELIST 重载相关模块。

    用 importlib.reload(config) 原地刷新，所有 `import config` 引用共享同一对象，
    避免旧服务模块仍绑旧 config 串台。
    """
    os.environ["RSTORY_TEST_MODE"] = test_mode
    os.environ["RSTORY_TEST_WHITELIST"] = whitelist
    import config

    importlib.reload(config)
    for mod in _SERVICE_MODS:
        sys.modules.pop(mod, None)
    from services import rstory_fsm_service as fsm
    from services import rstory_payment as payment
    from services import rstory_store as store

    return config, fsm, payment, store


async def _advance_to_first_payment_gate(fsm, store, uid):
    """推进到第一个 R 级 payment_gate（a_lina_gate_r）。"""
    await fsm.enter_story(uid, LINE_A, CHAR)
    await fsm.try_choice(uid, LINE_A, "talk_book")
    return await fsm.try_choice(uid, LINE_A, "team_up")


async def main() -> None:
    tmpdir = tempfile.mkdtemp(prefix="rstory_whitelist_smoke_")
    db_path = os.path.join(tmpdir, "rstory.sqlite3")
    os.environ["BOT_DB_PATH"] = os.path.join(tmpdir, "main.sqlite3")
    os.environ["RSTORY_DB_PATH"] = db_path
    os.environ.setdefault("RSTORY_PAYMENT_PROVIDER", "mock")

    WL_UID = 7256055877  # 云赫 @Pay9l，落在白名单内
    OUT_UID = 80001  # 白名单之外的普通用户

    # =================== A/B) TEST_MODE=False + 白名单仅含 WL_UID ===================
    config, fsm, payment, store = _reload("false", str(WL_UID))
    assert config.RSTORY_TEST_MODE is False, config.RSTORY_TEST_MODE
    assert WL_UID in config.RSTORY_TEST_WHITELIST, config.RSTORY_TEST_WHITELIST
    assert OUT_UID not in config.RSTORY_TEST_WHITELIST, config.RSTORY_TEST_WHITELIST

    # bypass 判定：白名单内 → True/whitelist；白名单外 → False。
    b_in, r_in = config.rstory_test_bypass(WL_UID)
    assert b_in is True and r_in == "whitelist", (b_in, r_in)
    assert config.rstory_test_bypass(str(WL_UID)) == (True, "whitelist")  # 容忍 str
    assert config.rstory_test_bypass(OUT_UID) == (False, ""), OUT_UID

    await store.init_store()
    mock = payment.MockUSDTProvider()
    payment.set_provider(mock)

    # ---- A) 白名单用户：payment_gate 放行 ----
    res = await _advance_to_first_payment_gate(fsm, store, WL_UID)
    assert res.status == fsm.STATUS_OK, res
    assert res.scene.scene_id == "a_lina_intimate", res.scene.scene_id
    assert await store.is_unlocked(WL_UID, "r_rated_lina") is True
    unlocks = await store._fetchall(
        "SELECT unlock_id, source FROM user_unlocks WHERE user_id = ?",
        (store._norm_uid(WL_UID),),
    )
    assert any(
        r["unlock_id"] == "r_rated_lina" and r["source"] == "test_mode" for r in unlocks
    ), unlocks
    # 没有走收款：未创建任何 rstory_charges。
    charges = await store._fetchall(
        "SELECT charge_id FROM rstory_charges WHERE user_id = ?",
        (str(store._norm_uid(WL_UID)),),
    )
    assert charges == [], charges
    # 数值/关系跃升照常。
    rel = await store.get_or_create_relation(WL_UID, CHAR)
    assert rel.relationship == "intimate", rel.relationship
    assert rel.flags.get("a_intimate_entered") is True, rel.flags
    print("[ok] 白名单用户(7256055877) TEST_MODE=False：payment_gate 放行（无 charge，source=test_mode），跃迁 intimate")

    # ---- A) 白名单用户：age_gate 也放行（视同已验证 + 审计） ----
    await fsm.try_choice(WL_UID, LINE_A, "linger")
    r_age = await fsm.try_choice(WL_UID, LINE_A, "confess")
    assert r_age.status == fsm.STATUS_OK, r_age
    assert r_age.scene.scene_id == "a_lina_devotion", r_age.scene.scene_id
    assert await store.is_age_verified(WL_UID) is True
    access = await store.list_content_access(WL_UID)
    assert any(r["age_verified"] for r in access), access
    assert await store.is_unlocked(WL_UID, "nsfw_lina") is True
    print("[ok] 白名单用户：age_gate 放行（视同已验证 + content_access_log 审计），nsfw payment 再放行")

    # ---- B) 白名单之外用户：维持正常收费 + 年龄验证 ----
    res_out = await _advance_to_first_payment_gate(fsm, store, OUT_UID)
    assert res_out.status == fsm.STATUS_NEEDS_UNLOCK, res_out
    assert res_out.unlock_id == "r_rated_lina", res_out
    assert await store.is_unlocked(OUT_UID, "r_rated_lina") is False
    # 走 mock create_charge 才解锁，source!=test_mode。
    charge_res = await payment.create_unlock_charge(
        OUT_UID, "r_rated_lina", provider=mock, script_id=LINE_A
    )
    assert charge_res.charge is not None, charge_res
    mock.mark_paid(charge_res.charge.charge_id)
    confirm = await payment.confirm_unlock(charge_res.charge.charge_id, provider=mock)
    assert confirm.ok and confirm.advance is not None, confirm
    src_rows = await store._fetchall(
        "SELECT source FROM user_unlocks WHERE user_id = ? AND unlock_id = ?",
        (store._norm_uid(OUT_UID), "r_rated_lina"),
    )
    assert src_rows and src_rows[0]["source"] != "test_mode", src_rows
    # 白名单外用户：age_gate 仍要求年龄验证（未验证 → NEEDS_AGE）。
    await fsm.try_choice(OUT_UID, LINE_A, "linger")
    r_age_out = await fsm.try_choice(OUT_UID, LINE_A, "confess")
    assert r_age_out.status == fsm.STATUS_NEEDS_AGE, r_age_out
    assert await store.is_age_verified(OUT_UID) is False
    print("[ok] 白名单外用户(80001) TEST_MODE=False：payment 仍 NEEDS_UNLOCK 走 create_charge；age 仍 NEEDS_AGE")

    await store.close_store()

    # =================== C) TEST_MODE=True 全员放行（含白名单外用户）===================
    config, fsm, payment, store = _reload("true", str(WL_UID))
    assert config.RSTORY_TEST_MODE is True, config.RSTORY_TEST_MODE
    # 全局开关命中：白名单外用户也 bypass，reason=global。
    assert config.rstory_test_bypass(OUT_UID) == (True, "global"), OUT_UID
    await store.init_store()
    mock = payment.MockUSDTProvider()
    payment.set_provider(mock)

    G_UID = 80002  # 全新白名单外用户，避免复用前面已有进度的 OUT_UID
    res_global = await _advance_to_first_payment_gate(fsm, store, G_UID)
    assert res_global.status == fsm.STATUS_OK, res_global
    assert res_global.scene.scene_id == "a_lina_intimate", res_global.scene.scene_id
    assert await store.is_unlocked(G_UID, "r_rated_lina") is True
    g_charges = await store._fetchall(
        "SELECT charge_id FROM rstory_charges WHERE user_id = ?",
        (str(store._norm_uid(G_UID)),),
    )
    assert g_charges == [], g_charges
    print("[ok] TEST_MODE=True：白名单外用户也全员放行（reason=global，无 charge，兼容原行为）")

    await store.close_store()

    # =================== D) 默认白名单含 7256055877 ===================
    # 不显式设 RSTORY_TEST_WHITELIST（清空 env）时，默认集合含 7256055877。
    os.environ.pop("RSTORY_TEST_WHITELIST", None)
    os.environ["RSTORY_TEST_MODE"] = "false"
    import config as _cfg

    importlib.reload(_cfg)
    assert 7256055877 in _cfg.RSTORY_TEST_WHITELIST, _cfg.RSTORY_TEST_WHITELIST
    assert _cfg.rstory_test_bypass(7256055877) == (True, "whitelist")
    assert _cfg.rstory_test_bypass(80001) == (False, "")
    print("[ok] 默认白名单含 7256055877（未设 env 时生效），其他用户默认不放行")

    print("\nALL RSTORY WHITELIST SMOKE TESTS PASSED")


def test_rstory_whitelist_smoke():
    asyncio.run(main())


if __name__ == "__main__":
    asyncio.run(main())
