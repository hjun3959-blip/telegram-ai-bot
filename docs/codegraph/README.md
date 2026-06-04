# CodeGraph (local dev tooling)

Static code-graph artifacts for the Telegram bot project, produced by `scripts/build_codegraph.py`. This is **development/maintenance tooling only** — it does not run, import, or alter the bot.

## How to regenerate

```bash
python scripts/build_codegraph.py     # or: make codegraph
```

Self-test (no network, writes to a temp dir):

```bash
python scripts/build_codegraph.py --self-test   # or: make codegraph-test
```

## Summary

- Generated at: `2026-05-31T15:52:58.574278+00:00`
- Modules scanned: **72**
- Internal import edges: **255**
- Detected aiogram handlers: **18**
- Important service-call edges: **40**

> Analysis is `ast`-only. No project code is imported; no `.env`, sqlite, or log files are read.

## aiogram routes / handlers

| Module | Handler | Events | Line |
| --- | --- | --- | --- |
| `routers.business` | `text_handler` | `router.business_message(F.text)` | 68 |
| `routers.media` | `photo_handler` | `router.message(F.photo)` | 570 |
| `routers.media` | `business_photo_handler` | `router.business_message(F.photo)` | 575 |
| `routers.media` | `voice_handler` | `router.message(F.voice)` | 671 |
| `routers.media` | `business_voice_handler` | `router.business_message(F.voice)` | 676 |
| `routers.media` | `video_handler` | `router.message(F.video)` | 690 |
| `routers.media` | `sticker_or_gif_handler` | `router.message(F.sticker | F.animation)` | 960 |
| `routers.media` | `business_sticker_or_gif_handler` | `router.business_message(F.sticker | F.animation)` | 965 |
| `routers.private` | `retry_command_handler` | `router.message(Command('继续'))` | 1102 |
| `routers.private` | `start_handler` | `router.message(CommandStart())` | 1114 |
| `routers.private` | `play_handler` | `router.message(Command('play'))` | 1135 |
| `routers.private` | `help_handler` | `router.message(Command('help'))` | 1153 |
| `routers.private` | `home_callback` | `router.callback_query(F.data.startswith('home:'))` | 1177 |
| `routers.private` | `play_callback` | `router.callback_query(F.data.startswith('play:'))` | 1253 |
| `routers.private` | `style_menu_callback` | `router.callback_query(F.data.startswith('style:'))` | 1290 |
| `routers.private` | `style_pick_callback` | `router.callback_query(F.data.startswith('stylepick:'))` | 1304 |
| `routers.private` | `beibei_companion_callback` | `router.callback_query(F.data.startswith('bb:'))` | 1398 |
| `routers.private` | `text_handler` | `router.message(F.text)` | 1567 |

## Important service edges

Call sites of key service functions (`call_openai`, `send_reply`, `store_message`, `sanitize*`).

### `call_openai`

- `routers.business` → `text_handler`
- `routers.media` → `_final_reply_via_core_model`
- `routers.media` → `_handle_photo`
- `routers.media` → `_handle_sticker_or_gif`
- `routers.media` → `_handle_voice`
- `routers.media` → `_visual_summary_via_vision`
- `routers.media` → `video_handler`
- `routers.private` → `_run_beibei_keyword_intent`
- `routers.private` → `text_handler`
- `scripts.smoke_test_openai_serial_retry` → `test_chat_lock_serial`
- `scripts.smoke_test_openai_serial_retry` → `test_no_retry_non_transient`
- `scripts.smoke_test_openai_serial_retry` → `test_retry_transient`
- `services.joke_service` → `_ai_generate_joke`
- `services.joke_service` → `_polish_with_model`

### `sanitize_visible_reply`

- `routers.business` → `text_handler`
- `routers.media` → `_sanitize_beibei_result`
- `routers.private` → `_run_atree_for_beibei`
- `routers.private` → `play_handler`
- `routers.private` → `start_handler`
- `routers.private` → `text_handler`
- `scripts.smoke_test_atree` → `test_persona_sanitize`
- `scripts.smoke_test_business_disclosure` → `test_sanitize_scrubs_robot_for_beibei`
- `services.atree_optimizer` → `rewrite_to_softer`
- `services.core_window_policy` → `_emergency_for_beibei`
- `services.daily_joke_scheduler` → `_sanitize_joke_for_beibei`

### `send_reply`

- `routers.business` → `text_handler`
- `routers.media` → `_handle_photo`
- `routers.media` → `_handle_sticker_or_gif`
- `routers.media` → `_handle_voice`
- `routers.media` → `video_handler`
- `routers.private` → `_run_beibei_keyword_intent`
- `routers.private` → `text_handler`

### `store_message`

- `routers.business` → `text_handler`
- `routers.media` → `_business_self_check`
- `routers.media` → `_handle_photo`
- `routers.media` → `_handle_sticker_or_gif`
- `routers.media` → `_handle_voice`
- `routers.media` → `_owner_self_sticker_or_gif_check`
- `routers.media` → `video_handler`
- `routers.private` → `text_handler`


## Module import graph

See `codegraph.mmd` (Mermaid) for a visual diagram. Edge list:

- `app` → `config`
- `app` → `db.core`
- `app` → `routers.business`
- `app` → `routers.media`
- `app` → `routers.private`
- `app` → `services.context_service`
- `app` → `services.daily_joke_scheduler`
- `app` → `utils.logger`
- `db.core` → `config`
- `db.core` → `utils.logger`
- `routers.business` → `config`
- `routers.business` → `services.alert_service`
- `routers.business` → `services.atree_keyword_trigger`
- `routers.business` → `services.atree_models`
- `routers.business` → `services.atree_owner_alert`
- `routers.business` → `services.atree_persona`
- `routers.business` → `services.atree_quote_library`
- `routers.business` → `services.atree_undo`
- `routers.business` → `services.beibei_keyword_trigger`
- `routers.business` → `services.business_memory_service`
- `routers.business` → `services.chat_action_service`
- `routers.business` → `services.companion_engine`
- `routers.business` → `services.companion_mode_router`
- `routers.business` → `services.context_service`
- `routers.business` → `services.filter_service`
- `routers.business` → `services.history_service`
- `routers.business` → `services.message_service`
- `routers.business` → `services.openai_service`
- `routers.business` → `services.reply_service`
- `routers.business` → `services.risk_alert_service`
- `routers.business` → `services.typing_delay_service`
- `routers.business` → `services.xiaopang_service`
- `routers.business` → `utils.logger`
- `routers.media` → `config`
- `routers.media` → `routers.private`
- `routers.media` → `services.alert_service`
- `routers.media` → `services.atree_models`
- `routers.media` → `services.atree_persona`
- `routers.media` → `services.business_memory_service`
- `routers.media` → `services.chat_action_service`
- `routers.media` → `services.context_service`
- `routers.media` → `services.history_service`
- `routers.media` → `services.media_service`
- `routers.media` → `services.message_service`
- `routers.media` → `services.openai_service`
- `routers.media` → `services.pending_style_service`
- `routers.media` → `services.plog_service`
- `routers.media` → `services.reply_service`
- `routers.media` → `services.self_media_service`
- `routers.media` → `services.typing_delay_service`
- `routers.media` → `services.xiaopang_service`
- `routers.media` → `utils.logger`
- `routers.private` → `config`
- `routers.private` → `services.alert_service`
- `routers.private` → `services.atree_keyword_trigger`
- `routers.private` → `services.atree_models`
- `routers.private` → `services.atree_owner_alert`
- `routers.private` → `services.atree_persona`
- `routers.private` → `services.atree_quote_library`
- `routers.private` → `services.atree_undo`
- `routers.private` → `services.beibei_companion_service`
- `routers.private` → `services.beibei_keyword_trigger`
- `routers.private` → `services.chat_action_service`
- `routers.private` → `services.companion_engine`
- `routers.private` → `services.companion_mode_router`
- `routers.private` → `services.contact_service`
- `routers.private` → `services.context_service`
- `routers.private` → `services.gray_status_service`
- `routers.private` → `services.history_service`
- `routers.private` → `services.image_generation_service`
- `routers.private` → `services.magnet_service`
- `routers.private` → `services.message_service`
- `routers.private` → `services.openai_service`
- `routers.private` → `services.pending_retry_service`
- `routers.private` → `services.pending_style_service`
- `routers.private` → `services.plan_service`
- `routers.private` → `services.plog_service`
- `routers.private` → `services.poster_service`
- `routers.private` → `services.reply_service`
- `routers.private` → `services.risk_alert_service`
- `routers.private` → `services.tool_command_service`
- `routers.private` → `services.xiaopang_service`
- `routers.private` → `services.y2k_service`
- `routers.private` → `utils.logger`
- `scripts.smoke_test_atree` → `services.atree_cooldown`
- `scripts.smoke_test_atree` → `services.atree_keyword_trigger`
- `scripts.smoke_test_atree` → `services.atree_optimizer`
- `scripts.smoke_test_atree` → `services.atree_outgoing_filter`
- `scripts.smoke_test_atree` → `services.atree_owner_alert`
- `scripts.smoke_test_atree` → `services.atree_persona`
- `scripts.smoke_test_atree` → `services.atree_privacy_filter`
- `scripts.smoke_test_atree` → `services.atree_quote_library`
- `scripts.smoke_test_atree` → `services.atree_undo`
- `scripts.smoke_test_atree_models` → `config`
- `scripts.smoke_test_atree_models` → `services`
- `scripts.smoke_test_atree_models` → `services.atree_models`
- `scripts.smoke_test_atree_models` → `services.atree_persona`
- `scripts.smoke_test_atree_models` → `services.core_window_policy`
- `scripts.smoke_test_beibei` → `config`
- `scripts.smoke_test_beibei` → `db.core`
- `scripts.smoke_test_beibei` → `routers.private`
- `scripts.smoke_test_beibei` → `services.reply_service`
- `scripts.smoke_test_beibei` → `services.self_media_service`
- `scripts.smoke_test_beibei` → `services.xiaopang_service`
- `scripts.smoke_test_beibei_media` → `config`
- `scripts.smoke_test_beibei_media` → `db.core`
- `scripts.smoke_test_beibei_media` → `routers.business`
- `scripts.smoke_test_beibei_media` → `routers.media`
- `scripts.smoke_test_beibei_media` → `routers.private`
- `scripts.smoke_test_beibei_media` → `services`
- `scripts.smoke_test_beibei_media` → `services.atree_models`
- `scripts.smoke_test_beibei_media` → `services.openai_service`
- `scripts.smoke_test_business_disclosure` → `config`
- `scripts.smoke_test_business_disclosure` → `db.core`
- `scripts.smoke_test_business_disclosure` → `routers.business`
- `scripts.smoke_test_business_disclosure` → `services.atree_persona`
- `scripts.smoke_test_companion_and_retry` → `config`
- `scripts.smoke_test_companion_and_retry` → `db.core`
- `scripts.smoke_test_companion_and_retry` → `routers.business`
- `scripts.smoke_test_companion_and_retry` → `routers.private`
- `scripts.smoke_test_companion_and_retry` → `services.alert_service`
- `scripts.smoke_test_companion_and_retry` → `services.atree_cooldown`
- `scripts.smoke_test_companion_and_retry` → `services.beibei_companion_service`
- `scripts.smoke_test_companion_and_retry` → `services.beibei_keyword_trigger`
- `scripts.smoke_test_companion_and_retry` → `services.companion_engine`
- `scripts.smoke_test_companion_and_retry` → `services.companion_mode_router`
- `scripts.smoke_test_companion_and_retry` → `services.pending_retry_service`
- `scripts.smoke_test_companion_and_retry` → `services.plog_service`
- `scripts.smoke_test_companion_and_retry` → `services.risk_alert_service`
- `scripts.smoke_test_companion_and_retry` → `services.xiaopang_service`
- `scripts.smoke_test_contact_only` → `config`
- `scripts.smoke_test_contact_only` → `db.core`
- `scripts.smoke_test_contact_only` → `routers.business`
- `scripts.smoke_test_contact_only` → `routers.private`
- `scripts.smoke_test_contact_only` → `services.contact_service`
- `scripts.smoke_test_daily_joke` → `config`
- `scripts.smoke_test_daily_joke` → `db.core`
- `scripts.smoke_test_daily_joke` → `services.daily_joke_scheduler`
- `scripts.smoke_test_daily_joke` → `services.joke_service`
- `scripts.smoke_test_daily_joke` → `services.xiaopang_service`
- `scripts.smoke_test_entertainment_menu` → `config`
- `scripts.smoke_test_entertainment_menu` → `db.core`
- `scripts.smoke_test_entertainment_menu` → `routers.media`
- `scripts.smoke_test_entertainment_menu` → `routers.private`
- `scripts.smoke_test_entertainment_menu` → `services.pending_style_service`
- `scripts.smoke_test_entertainment_menu` → `services.plog_service`
- `scripts.smoke_test_joke_sanitize` → `services.daily_joke_scheduler`
- `scripts.smoke_test_media_sanitize` → `routers.media`
- `scripts.smoke_test_openai_serial_retry` → `services`
- `scripts.smoke_test_openai_serial_retry` → `services.openai_service`
- `scripts.smoke_test_owner_health` → `config`
- `scripts.smoke_test_owner_health` → `db.core`
- `scripts.smoke_test_owner_health` → `routers.private`
- `scripts.smoke_test_owner_health` → `services.gray_status_service`
- `scripts.smoke_test_plog` → `config`
- `scripts.smoke_test_plog` → `db.core`
- `scripts.smoke_test_plog` → `routers.media`
- `scripts.smoke_test_plog` → `routers.private`
- `scripts.smoke_test_plog` → `services.image_generation_service`
- `scripts.smoke_test_plog` → `services.magnet_service`
- `scripts.smoke_test_plog` → `services.plog_service`
- `scripts.smoke_test_plog` → `services.poster_service`
- `scripts.smoke_test_plog` → `services.y2k_service`
- `scripts.smoke_test_silence_and_delay` → `config`
- `scripts.smoke_test_silence_and_delay` → `routers.business`
- `scripts.smoke_test_silence_and_delay` → `routers.media`
- `scripts.smoke_test_silence_and_delay` → `routers.private`
- `scripts.smoke_test_silence_and_delay` → `services.context_service`
- `scripts.smoke_test_silence_and_delay` → `services.typing_delay_service`
- `scripts.smoke_test_y2k_poster` → `config`
- `scripts.smoke_test_y2k_poster` → `db.core`
- `scripts.smoke_test_y2k_poster` → `routers.media`
- `scripts.smoke_test_y2k_poster` → `routers.private`
- `scripts.smoke_test_y2k_poster` → `services.plog_service`
- `scripts.smoke_test_y2k_poster` → `services.poster_service`
- `scripts.smoke_test_y2k_poster` → `services.y2k_service`
- `services.alert_service` → `config`
- `services.alert_service` → `utils.logger`
- `services.atree_models` → `config`
- `services.atree_optimizer` → `services.atree_persona`
- `services.atree_outgoing_filter` → `services.atree_optimizer`
- `services.atree_owner_alert` → `services.atree_cooldown`
- `services.atree_owner_alert` → `services.atree_keyword_trigger`
- `services.atree_owner_alert` → `services.atree_privacy_filter`
- `services.atree_privacy_filter` → `services.atree_keyword_trigger`
- `services.atree_quote_library` → `services.atree_persona`
- `services.beibei_companion_service` → `services.alert_service`
- `services.beibei_companion_service` → `services.companion_engine`
- `services.beibei_companion_service` → `services.companion_mode_router`
- `services.beibei_companion_service` → `services.reply_service`
- `services.beibei_companion_service` → `services.xiaopang_service`
- `services.beibei_companion_service` → `utils.logger`
- `services.beibei_keyword_trigger` → `services.companion_mode_router`
- `services.business_memory_service` → `services.history_service`
- `services.chat_action_service` → `utils.logger`
- `services.companion_engine` → `services.companion_mode_router`
- `services.companion_mode_router` → `services.risk_alert_service`
- `services.contact_service` → `config`
- `services.contact_service` → `db.core`
- `services.contact_service` → `services.context_service`
- `services.contact_service` → `utils.logger`
- `services.context_service` → `config`
- `services.context_service` → `utils.logger`
- `services.core_window_policy` → `services.atree_models`
- `services.core_window_policy` → `services.atree_persona`
- `services.daily_joke_scheduler` → `config`
- `services.daily_joke_scheduler` → `services.alert_service`
- `services.daily_joke_scheduler` → `services.atree_persona`
- `services.daily_joke_scheduler` → `services.joke_service`
- `services.daily_joke_scheduler` → `services.xiaopang_service`
- `services.daily_joke_scheduler` → `utils.logger`
- `services.filter_service` → `config`
- `services.gray_status_service` → `config`
- `services.gray_status_service` → `db.core`
- `services.gray_status_service` → `services.atree_models`
- `services.gray_status_service` → `services.atree_persona`
- `services.gray_status_service` → `services.xiaopang_service`
- `services.gray_status_service` → `utils.logger`
- `services.history_service` → `config`
- `services.image_generation_service` → `config`
- `services.image_generation_service` → `services.openai_service`
- `services.image_generation_service` → `utils.logger`
- `services.joke_service` → `config`
- `services.joke_service` → `services.openai_service`
- `services.joke_service` → `utils.logger`
- `services.magnet_service` → `services.image_generation_service`
- `services.magnet_service` → `utils.logger`
- `services.message_service` → `db.core`
- `services.message_service` → `services.context_service`
- `services.message_service` → `utils.logger`
- `services.openai_service` → `config`
- `services.openai_service` → `utils.logger`
- `services.plan_service` → `db.core`
- `services.plog_service` → `services.image_generation_service`
- `services.plog_service` → `utils.logger`
- `services.poster_service` → `services.image_generation_service`
- `services.poster_service` → `utils.logger`
- `services.reply_service` → `config`
- `services.reply_service` → `utils.logger`
- `services.risk_alert_service` → `services.alert_service`
- `services.risk_alert_service` → `utils.logger`
- `services.self_media_service` → `db.core`
- `services.self_media_service` → `utils.logger`
- `services.tool_command_service` → `config`
- `services.tool_command_service` → `services.openai_service`
- `services.tool_command_service` → `utils.logger`
- `services.typing_delay_service` → `config`
- `services.typing_delay_service` → `services.chat_action_service`
- `services.typing_delay_service` → `utils.logger`
- `services.xiaopang_service` → `config`
- `services.xiaopang_service` → `db.core`
- `services.xiaopang_service` → `services.alert_service`
- `services.xiaopang_service` → `services.context_service`
- `services.y2k_service` → `services.image_generation_service`
- `services.y2k_service` → `utils.logger`

---

_Generated by `scripts/build_codegraph.py`. Safe to commit; dev-only._
