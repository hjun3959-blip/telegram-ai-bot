# 贝贝 / 小胖 Business 媒体模板化问题修复报告

## 一、目标回顾

- 贝贝 Business 文字已经走 `CORE_MODEL=gpt-5.5`，但媒体（图片、贴纸、GIF）原来硬走 `VISION_MODEL=gemini-3.1-flash-lite`，最终回复由 Gemini 出，且没有聊天历史，导致模板化（“看到这个心情都亮了”等套话）。
- 本轮修复必须保证：贝贝/联系人 Business 的媒体最终回复必须由 `CORE_MODEL=gpt-5.5` 主脑产出；Gemini 只承担可选的“客观视觉摘要”中间角色。
- 不部署服务器，仅完成代码 + 测试。

## 二、修改文件清单

| 文件 | 性质 | 说明 |
| --- | --- | --- |
| `services/business_memory_service.py` | **新增** | 把 business `user_histories` 抽到独立模块，供 `routers/business.py` 与 `routers/media.py` 共享。提供 `get_history` / `save_history` / `trim` / `clear`，并以 `user_histories` 别名兼容旧引用，避免循环 import。 |
| `routers/business.py` | 改写 | 删除内部 `user_histories` / `get_history` / `save_history`，改为从 `services.business_memory_service` 导入。保留 `user_histories` 同名引用以保护历史测试。 |
| `services/openai_service.py` | 增强 | `call_openai` / `_do_chat` 新增 `response_json: bool = True` 参数。设为 `False` 时**不**附 `response_format=json_object`，直接返回 plain text；fallback 在 `response_json=False` 下返回空字符串而不是默认 dict，调用方自己兜底。 |
| `routers/media.py` | 重构 photo + sticker/GIF | 见下方“核心逻辑”一节。 |
| `scripts/smoke_test_beibei_media.py` | **新增** | 覆盖 business memory 共享、CORE_MODEL 路由、save_history、file_id 不外泄、反模板化禁词、非联系人静默仍先于模型等。 |

## 三、核心逻辑

### 1. 共享会话历史 `services/business_memory_service.py`

- 模块级 `_user_histories: dict[int, list[dict]]`。
- `save_history(user_id, user_content, assistant_reply)`：
  - 同时为空才跳过；
  - 任一非空都会写入 `{"role":"user"}` + `{"role":"assistant"}`，再用既有 `history_service.trim_history` 双限制 trim。
  - 媒体场景的 `user_content` 是已脱敏的“[图片]：caption”、“[贴纸表情] emoji=😂、贴纸集=…”这类人话占位，**不含 file_id**。
- `get_history` 返回浅拷贝，调用方修改不会污染 store。
- 暴露 `user_histories = _user_histories` 以兼容老代码 / 老测试中的 `routers.business.user_histories` 引用。

### 2. `services/openai_service.call_openai`

```python
async def call_openai(messages, model, mode, response_json: bool = True):
    ...
```

- 默认行为不变（仍走 `response_format=json_object` + `_normalize_result`），所以**所有现有调用都向后兼容**。
- `response_json=False`：直接 `response.choices[0].message.content.strip()` 返回字符串；主模型/备份模型失败时返回空字符串。
- 视觉摘要专用 `response_json=False`，避免在没法走 JSON 的中间分析步骤上强制 JSON。

### 3. 图片 `_handle_photo`

业务（贝贝/联系人）流程改成**两段式**：

1. **第一段（视觉摘要，可选 Gemini）**：`_visual_summary_via_vision()` 把图片喂给 `VISION_MODEL=gemini-3.1-flash-lite`，`response_json=False`。system prompt 明确：
   - “你只负责看图出摘要，不负责聊天”
   - 不允许第一人称语气、不准产出 JSON、不准提到 file_id / set_name / URL / base64 等素材 ID
   - 输出 ≤5 句中文，纯描述
2. **第二段（最终回复，必须 gpt-5.5）**：`_final_reply_via_core_model()` 拼装：
   - 当前 `system_prompt_for_mode(message)` 再过 `build_system_prompt_with_xiaopang`（自动注入贝贝画像 + 情绪雷达）
   - 用户 prompt：
     - 视觉摘要（标注“仅供你参考，不要复述”）
     - 用户随媒体发的 caption（如果有）
     - 最近 ≤6 轮历史（来自 `business_memory_service`），格式 `对方：…\n我：…`
     - 任务要求“紧贴上一句、媒体在表达的东西、对方情绪”
     - **反模板化硬红线 `_ANTI_TEMPLATE_BAN_LIST`**：明示禁止“看到这个心情都亮了/好起来了/这张图太可爱了/收到你发的图…”等 9 条万能模板
   - `call_openai(messages, CORE_MODEL, "business")` → JSON
3. **静默 & save_history**：
   - 不论是否真发，都会 `biz_save_history(user_id, "[图片]：caption", reply_text)`（静默时 assistant 写 ""），保证下一轮文本能看到“刚才对方发了图、我回了/没回”。
   - 静默判定仍走原 `_should_reply_business`。
4. **send_reply 的 model 参数从 `VISION_MODEL` 改成 `CORE_MODEL`**，日志层面也能直接看到“最终走 gpt-5.5”。
5. **private 模式不动**：仍由 VISION 直接出 JSON（保持功能区表现），符合需求第 4 条“不要破坏现有联系人静默 / cooldown / 真人延迟 / 素材采集”。

### 4. 贴纸 / GIF `_handle_sticker_or_gif`

- 不再下载或上传图片到任何模型（贴纸/动图本来也不该跑 VISION）。
- Business 必走 `_final_reply_via_core_model(..., media_kind="GIF动图" or "贴纸表情", visual_or_human_summary=human_desc)`，`human_desc` 来自既有 `_describe_incoming_sticker_or_gif` 的“贴纸、emoji=…、贴纸集=…、类型=…”人话拼接，**不含 file_id**。
- private 模式保留原 `choose_model` + `_build_sticker_user_prompt` 流程，行为不变。
- 同样在静默 / 真回复两种路径上都 `biz_save_history(user_id, "[贴纸表情] …" / "[GIF动图] …", reply_or_empty)`，让下轮文本知道“对方刚发了贴纸”。
- 弹药库挑选、`reuse_in_same_turn=false` 限制、send_reply 反 echo 全部保留；`model` 参数对应改成 `CORE_MODEL`。

### 5. 反模板化

`_ANTI_TEMPLATE_BAN_LIST` 注入到最终 user prompt：
```
"看到这个心情都亮了", "看到这个心情都好起来了", "一看到这个就笑了",
"这张图太可爱了", "谢谢你分享这张图", "收到你发的图",
"收到你的贴纸", "多么可爱的表情", "看到你发这个"
```
+ 还有“不要描述图本身/贴纸本身长什么样” + “要紧贴上一句、图里在表达的东西、贝贝当下情绪”。

视觉摘要 system prompt 还独立加了一条：**不要出现“file_id”/“set_name”/“贴纸集”/URL/base64**，杜绝素材 ID 泄漏到中间产物。

### 6. 联系人 / cooldown / 拟真延迟 / 素材采集 都未动

- `_business_self_check`、`_business_non_contact_check`、`record_incoming_media`、`record_self_media`、`pick_media_asset`、`STICKER_MAP` 全部保留。
- 拟真延迟 `human_typing_delay` 与 owner cooldown 在 photo / sticker / GIF 的发送前依旧检查。
- 非联系人 photo 仍**先于模型**返回（VISION 与 CORE 都不会被调）——已在 `smoke_test_beibei_media.py` 第 10 项断言验证。

## 四、测试结果

### 1. 编译

```
$ python3 -m compileall -q /home/user/workspace/project_phase1_1
（无输出 = 全部 OK）
```

### 2. 既有 smoke 测试（确保未回归）

```
$ python3 scripts/smoke_test_beibei.py
ALL SMOKE TESTS PASSED

$ python3 scripts/smoke_test_contact_only.py
ALL CONTACT-ONLY SMOKE TESTS PASSED

$ python3 scripts/smoke_test_silence_and_delay.py
ALL SILENCE + DELAY SMOKE TESTS PASSED
```

### 3. 新增 smoke 测试

```
$ python3 scripts/smoke_test_beibei_media.py
[ok] business_memory_service: get/save/trim/clear 行为正常
[ok] routers.business 改用共享 memory service，旧 user_histories 仍兼容
[ok] services.openai_service.call_openai 支持 response_json 参数
[ok] routers/media._visual_summary_via_vision 使用 plain text 模式
[ok] 贝贝 photo 最终 call_openai 走 CORE_MODEL=gpt-5.5
[ok] 贝贝 photo 处理后 save_history 写入：[{'role': 'user', 'content': '[图片]：看我家猫'}, ...]
[ok] 贝贝 sticker 最终 call_openai 走 CORE_MODEL=gpt-5.5
[ok] 贝贝 sticker 处理后 save_history 写入：[{'role': 'user', 'content': '[贴纸表情] 贴纸、emoji=😂、贴纸集=someset、类型=regular'}, ...]
[ok] 贝贝 GIF 同样走 CORE_MODEL 并入 save_history
[ok] 视觉摘要不暴露 file_id；最终 prompt 含反模板化禁词
[ok] 非联系人 photo 静默先于模型生效（VISION 也不调）
[ok] /play /help 文案未回归

ALL BEIBEI-MEDIA SMOKE TESTS PASSED
```

## 五、影响面与回滚

- 修改集中在 1 个新 service、1 个新 smoke 测试、1 个 router 重构、1 个底层服务增强。Tower / private 路由、xiaopang 服务、媒体素材库等都未触动。
- 单点回滚：把 `routers/media.py` revert 即可恢复原 VISION 直出行为；但 `services/openai_service.py` 与 `services/business_memory_service.py` 的新增能力**完全后向兼容**（新参数默认值 + 共享 dict 与旧 dict 同对象）。
