"""Smoke test：OxaPay 真实支付 provider + Webhook 验签（数据驱动解锁模型，不联网）。

覆盖：
1) OxaPayProvider.create_charge 请求 URL/头/体字段正确（merchant_api_key 头、amount、
   currency=USD、order_id=charge_id、callback_url、lifetime、sandbox），解析 track_id/
   payment_url 落库（含 create_unlock_charge 编排，金额取自 unlock_products USDT 价）。
2) Webhook 验签：HMAC-SHA512 正确签名通过、错误/缺失/篡改签名拒绝。
3) paid 回调触发幂等解锁（写 user_unlocks）；重复投递不重复解锁、confirmed_at 不变。
4) 非 paid 状态（waiting / failed）不解锁。
5) 金额/币种不匹配的回调被拒（400）。
6) order_id 缺失时按 track_id 兜底定位。
7) provider 注册表含 oxapay + mock。

HTTP 用 monkeypatch 假 aiohttp.ClientSession，不发真实网络。DB 用临时文件。
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


class _FakeResponse:
    def __init__(self, status: int, body: dict):
        self.status = status
        self._body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def text(self):
        return json.dumps(self._body)

    async def json(self, content_type=None):
        return self._body


class _FakeSession:
    """记录最后一次 post 的 url/json/headers，返回预设响应。"""

    last_call: dict = {}
    response_body: dict = {}
    response_status: int = 200

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def post(self, url, json=None, headers=None):
        _FakeSession.last_call = {"url": url, "json": json, "headers": headers}
        return _FakeResponse(_FakeSession.response_status, _FakeSession.response_body)


async def main() -> None:
    tmpdir = tempfile.mkdtemp(prefix="oxapay_smoke_")
    db_path = os.path.join(tmpdir, "rstory.sqlite3")
    os.environ["BOT_DB_PATH"] = os.path.join(tmpdir, "main.sqlite3")
    os.environ["RSTORY_DB_PATH"] = db_path
    os.environ["RSTORY_PAYMENT_PROVIDER"] = "oxapay"
    os.environ["OXAPAY_MERCHANT_API_KEY"] = "test_merchant_key_PLACEHOLDER"
    os.environ["OXAPAY_CALLBACK_BASE_URL"] = "https://bot.example.com"
    os.environ["OXAPAY_CALLBACK_PATH"] = "/rstory/oxapay/webhook"
    os.environ["OXAPAY_SANDBOX"] = "true"
    os.environ["OXAPAY_INVOICE_LIFETIME_MIN"] = "60"

    for mod in (
        "config",
        "services.rstory_store",
        "services.rstory_fsm_service",
        "services.rstory_payment",
        "services.rstory_webhook",
    ):
        sys.modules.pop(mod, None)

    import config
    from services import rstory_fsm_service as fsm
    from services import rstory_payment as payment
    from services import rstory_store as store
    from services import rstory_webhook as webhook

    payment.aiohttp.ClientSession = _FakeSession

    await store.init_store()
    script_id = fsm.DEFAULT_SCRIPT_ID
    merchant_key = config.OXAPAY_MERCHANT_API_KEY

    # ---------- 1) create_charge 请求字段 + 解析落库（金额取自 unlock_products）----------
    _FakeSession.response_status = 200
    _FakeSession.response_body = {
        "data": {
            "track_id": "TRACK_1001",
            "payment_url": "https://oxapay.com/pay/abc123",
            "expired_at": 1999999999,
        }
    }
    provider = payment.OxaPayProvider()
    payment.set_provider(provider)

    uid = 70001
    # nsfw_char_luna 定价 3.0（来自 unlock_products.usdt_amount）
    res = await payment.create_unlock_charge(uid, "nsfw_char_luna", provider=provider)
    assert res.already_unlocked is False and res.charge is not None, res
    charge_id = res.charge.charge_id
    assert charge_id.startswith("oxapay_"), charge_id
    assert res.charge.payment_url == "https://oxapay.com/pay/abc123", res.charge
    assert res.charge.track_id == "TRACK_1001", res.charge

    call = _FakeSession.last_call
    assert call["url"].endswith("/payment/invoice"), call["url"]
    assert call["headers"]["merchant_api_key"] == merchant_key
    assert call["headers"]["Content-Type"] == "application/json"
    body = call["json"]
    assert body["amount"] == 3.0, body
    assert body["currency"] == "USD", body
    assert body["order_id"] == charge_id, body
    assert body["lifetime"] == 60, body
    assert body["sandbox"] is True, body
    assert body["callback_url"] == "https://bot.example.com/rstory/oxapay/webhook", body
    assert merchant_key not in json.dumps(body), "merchant key 不应进 body"

    rec = await store.get_charge(charge_id)
    assert rec is not None and rec.status == store.CHARGE_PENDING
    assert rec.unlock_id == "nsfw_char_luna"
    assert rec.track_id == "TRACK_1001" and rec.payment_url == "https://oxapay.com/pay/abc123"
    print("[ok] create_charge 请求字段正确 + USDT 价取自 unlock_products + track_id/payment_url 落库")

    # ---------- 2) Webhook 验签 ----------
    paid_payload = {
        "type": "invoice",
        "status": "Paid",
        "amount": 3.0,
        "currency": "USD",
        "track_id": "TRACK_1001",
        "order_id": charge_id,
    }
    raw = json.dumps(paid_payload).encode("utf-8")
    good_sig = hmac.new(merchant_key.encode("utf-8"), raw, hashlib.sha512).hexdigest()

    assert webhook.verify_signature(raw, good_sig, merchant_key) is True
    assert webhook.verify_signature(raw, "deadbeef", merchant_key) is False
    assert webhook.verify_signature(raw, None, merchant_key) is False
    assert webhook.verify_signature(raw, good_sig, "") is False
    tampered = json.dumps({**paid_payload, "amount": 999}).encode("utf-8")
    assert webhook.verify_signature(tampered, good_sig, merchant_key) is False
    print("[ok] Webhook HMAC-SHA512 验签：正确通过 / 错误/缺失/篡改 拒绝")

    status, resp_body = await webhook.process_webhook(raw, "wrongsig", secret=merchant_key)
    assert status == 401, (status, resp_body)
    assert not await store.is_unlocked(uid, "nsfw_char_luna")
    print("[ok] 错误签名的回调返回 401 且不解锁")

    # ---------- 3) paid 回调幂等解锁 ----------
    status, resp_body = await webhook.process_webhook(raw, good_sig, secret=merchant_key)
    assert status == 200 and resp_body == "ok", (status, resp_body)
    assert await store.is_unlocked(uid, "nsfw_char_luna")
    rec2 = await store.get_charge(charge_id)
    assert rec2.status == store.CHARGE_PAID and rec2.confirmed_at is not None, rec2
    first_confirmed_at = rec2.confirmed_at
    # 解锁来源默认 oxapay
    unlocked_rows = await store.list_unlocked(uid)
    assert unlocked_rows == ["nsfw_char_luna"], unlocked_rows

    status, resp_body = await webhook.process_webhook(raw, good_sig, secret=merchant_key)
    assert status == 200 and resp_body == "ok"
    assert await store.list_unlocked(uid) == ["nsfw_char_luna"]
    rec3 = await store.get_charge(charge_id)
    assert rec3.confirmed_at == first_confirmed_at, "重复回调不应刷新 confirmed_at"
    print("[ok] paid 回调幂等解锁：重复投递不重复解锁、confirmed_at 不变")

    # ---------- 4) 非 paid 状态不解锁 ----------
    _FakeSession.response_body = {
        "data": {"track_id": "TRACK_2002", "payment_url": "https://oxapay.com/pay/def456"}
    }
    res3 = await payment.create_unlock_charge(uid, "devoted_char_luna", provider=provider)
    assert res3.charge.usdt_amount == 5.0, res3.charge
    charge3 = res3.charge.charge_id

    waiting_payload = {
        "type": "invoice", "status": "Waiting", "amount": 5.0,
        "currency": "USD", "order_id": charge3, "track_id": "TRACK_2002",
    }
    raw_w = json.dumps(waiting_payload).encode("utf-8")
    sig_w = hmac.new(merchant_key.encode("utf-8"), raw_w, hashlib.sha512).hexdigest()
    status, _ = await webhook.process_webhook(raw_w, sig_w, secret=merchant_key)
    assert status == 200
    assert not await store.is_unlocked(uid, "devoted_char_luna"), "waiting 不应解锁"
    rec_w = await store.get_charge(charge3)
    assert rec_w.status == store.CHARGE_PENDING, rec_w.status

    failed_payload = {**waiting_payload, "status": "Failed"}
    raw_f = json.dumps(failed_payload).encode("utf-8")
    sig_f = hmac.new(merchant_key.encode("utf-8"), raw_f, hashlib.sha512).hexdigest()
    status, _ = await webhook.process_webhook(raw_f, sig_f, secret=merchant_key)
    assert status == 200
    assert not await store.is_unlocked(uid, "devoted_char_luna")
    rec_f = await store.get_charge(charge3)
    assert rec_f.status == store.CHARGE_FAILED, rec_f.status
    print("[ok] 非 paid 状态（waiting / failed）不解锁，failed 落库为 failed")

    # ---------- 5) 金额/币种不匹配被拒 ----------
    bad_amount = {**paid_payload, "order_id": charge3, "track_id": "TRACK_2002", "amount": 1.0}
    raw_ba = json.dumps(bad_amount).encode("utf-8")
    sig_ba = hmac.new(merchant_key.encode("utf-8"), raw_ba, hashlib.sha512).hexdigest()
    status, resp_body = await webhook.process_webhook(raw_ba, sig_ba, secret=merchant_key)
    assert status == 400, (status, resp_body)
    assert not await store.is_unlocked(uid, "devoted_char_luna")

    bad_currency = {
        "type": "invoice", "status": "Paid", "amount": 5.0, "currency": "EUR",
        "order_id": charge3, "track_id": "TRACK_2002",
    }
    raw_bc = json.dumps(bad_currency).encode("utf-8")
    sig_bc = hmac.new(merchant_key.encode("utf-8"), raw_bc, hashlib.sha512).hexdigest()
    status, _ = await webhook.process_webhook(raw_bc, sig_bc, secret=merchant_key)
    assert status == 400
    assert not await store.is_unlocked(uid, "devoted_char_luna")
    print("[ok] 金额/币种不匹配的回调被拒（400）且不解锁")

    # ---------- 6) order_id 缺失时用 track_id 兜底定位 ----------
    paid_by_track = {
        "type": "invoice", "status": "paid", "amount": 5.0, "currency": "usd",
        "track_id": "TRACK_2002",
    }
    raw_t = json.dumps(paid_by_track).encode("utf-8")
    sig_t = hmac.new(merchant_key.encode("utf-8"), raw_t, hashlib.sha512).hexdigest()
    status, resp_body = await webhook.process_webhook(raw_t, sig_t, secret=merchant_key)
    assert status == 200 and resp_body == "ok", (status, resp_body)
    assert await store.is_unlocked(uid, "devoted_char_luna")
    print("[ok] order_id 缺失时按 track_id 兜底定位 + 小写 paid/usd 也认")

    # ---------- 7) provider 注册 ----------
    assert "oxapay" in payment._PROVIDER_REGISTRY
    assert payment._PROVIDER_REGISTRY["oxapay"] is payment.OxaPayProvider
    assert "mock" in payment._PROVIDER_REGISTRY, "Mock 应保留作回退"
    print("[ok] OxaPayProvider 已注册到 _PROVIDER_REGISTRY（mock 保留回退）")

    await store.close_store()
    print("\nALL OXAPAY SMOKE TESTS PASSED")


def test_oxapay_smoke():
    asyncio.run(main())


if __name__ == "__main__":
    asyncio.run(main())
