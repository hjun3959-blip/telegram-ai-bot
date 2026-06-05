-- R 级互动剧情系统 —— 数据驱动 schema + 种子数据（幂等）。
--
-- 由 services/rstory_store.py 在启动时用 executescript 跑（建表 + 种子）。
-- 全部 CREATE TABLE IF NOT EXISTS / INSERT OR IGNORE，可重复执行不报错、不重复插。
--
-- 与上传蓝本（rstory_refs/init_seed.sql）的差异（用户最终决定）：
-- 1) 统一 USDT 计价：unlock_products 增加 usdt_amount 列（REAL），r_rated=2 / nsfw_char_luna=3 /
--    devoted_char_luna=5；保留 stars 列仅作历史兼容（不再用于计价）。代码层只读 usdt_amount。
-- 2) user_unlocks.source 默认 'oxapay'（不再是 'stars'）。
-- 3) 不建独立 users 表的主键冲突：users 已在主库其它逻辑外，这里 IF NOT EXISTS 自包含，
--    age_verified 字段是分级合规的权威来源。

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    username TEXT,
    age_verified INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS scripts (
    script_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    description TEXT,
    entry_state TEXT NOT NULL DEFAULT 'scene_intro',
    is_active INTEGER DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS characters (
    char_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    base_prompt TEXT NOT NULL,
    r_prompt TEXT,
    nsfw_prompt TEXT,
    devoted_prompt TEXT,
    content_level INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS script_characters (
    script_id TEXT NOT NULL,
    char_id TEXT NOT NULL,
    role_name TEXT NOT NULL,
    PRIMARY KEY (script_id, char_id),
    FOREIGN KEY (script_id) REFERENCES scripts(script_id),
    FOREIGN KEY (char_id) REFERENCES characters(char_id)
);

CREATE TABLE IF NOT EXISTS scenes (
    scene_id TEXT PRIMARY KEY,
    script_id TEXT NOT NULL,
    state_type TEXT NOT NULL,
    scene_type TEXT NOT NULL,
    title TEXT,
    fixed_text TEXT,
    choices_json TEXT DEFAULT '[]',
    content_level INTEGER DEFAULT 0,
    char_id TEXT,
    FOREIGN KEY (script_id) REFERENCES scripts(script_id),
    FOREIGN KEY (char_id) REFERENCES characters(char_id)
);

CREATE TABLE IF NOT EXISTS user_game_state (
    user_id INTEGER NOT NULL,
    script_id TEXT NOT NULL,
    current_fsm_state TEXT NOT NULL,
    current_char_id TEXT,
    history_json TEXT DEFAULT '[]',
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, script_id),
    FOREIGN KEY (script_id) REFERENCES scripts(script_id),
    FOREIGN KEY (current_char_id) REFERENCES characters(char_id)
);

CREATE TABLE IF NOT EXISTS user_char_relation (
    user_id INTEGER NOT NULL,
    char_id TEXT NOT NULL,
    affection INTEGER DEFAULT 50,
    trust INTEGER DEFAULT 30,
    desire INTEGER DEFAULT 0,
    dominance INTEGER DEFAULT 50,
    relationship TEXT DEFAULT 'stranger',
    current_mood TEXT DEFAULT 'neutral',
    flags TEXT DEFAULT '{}',
    total_messages INTEGER DEFAULT 0,
    last_active TIMESTAMP,
    PRIMARY KEY (user_id, char_id),
    FOREIGN KEY (char_id) REFERENCES characters(char_id)
);

CREATE TABLE IF NOT EXISTS fsm_transitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    script_id TEXT NOT NULL,
    from_state TEXT NOT NULL,
    to_state TEXT NOT NULL,
    trigger_type TEXT NOT NULL,
    trigger_value TEXT,
    condition_json TEXT,
    effect_json TEXT,
    priority INTEGER DEFAULT 0,
    FOREIGN KEY (script_id) REFERENCES scripts(script_id)
);

CREATE TABLE IF NOT EXISTS unlock_products (
    unlock_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    description TEXT,
    content_level INTEGER NOT NULL,
    stars INTEGER NOT NULL,
    usdt_amount REAL NOT NULL DEFAULT 0,
    char_id TEXT,
    is_active INTEGER DEFAULT 1,
    FOREIGN KEY (char_id) REFERENCES characters(char_id)
);

CREATE TABLE IF NOT EXISTS user_unlocks (
    user_id INTEGER NOT NULL,
    unlock_id TEXT NOT NULL,
    unlocked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    source TEXT DEFAULT 'oxapay',
    charge_id TEXT,
    PRIMARY KEY (user_id, unlock_id),
    FOREIGN KEY (unlock_id) REFERENCES unlock_products(unlock_id)
);

CREATE TABLE IF NOT EXISTS stat_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    char_id TEXT NOT NULL,
    stat_name TEXT NOT NULL,
    delta INTEGER NOT NULL,
    reason TEXT,
    scene_id TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS content_access_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    content_level INTEGER NOT NULL,
    scene_id TEXT,
    age_verified INTEGER DEFAULT 0,
    accessed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 支付订单/对账表：复用 OxaPay 既有逻辑（track_id / payment_url / status 流转）。
-- 与上传 schema 无冲突；解锁产品维度用 unlock_id（替代旧 character+stage）。
CREATE TABLE IF NOT EXISTS rstory_charges (
    charge_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    unlock_id TEXT NOT NULL,
    usdt_amount REAL NOT NULL,
    provider TEXT NOT NULL,
    status TEXT NOT NULL,
    pay_address TEXT,
    pay_info TEXT,
    track_id TEXT,
    payment_url TEXT,
    created_at TEXT NOT NULL,
    confirmed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_rstory_charges_user ON rstory_charges(user_id, unlock_id);
CREATE INDEX IF NOT EXISTS idx_rstory_charges_track ON rstory_charges(track_id);
CREATE INDEX IF NOT EXISTS idx_user_unlocks_user ON user_unlocks(user_id);
CREATE INDEX IF NOT EXISTS idx_fsm_transitions_from ON fsm_transitions(script_id, from_state);

-- ===================== 种子数据（demo_mansion / Luna）=====================

INSERT OR IGNORE INTO scripts (script_id, title, description, entry_state) VALUES
('demo_mansion', '雾宅试炼', '用于验证 FSM、分级、支付门、年龄门的最小可运行示例。', 'scene_intro');

INSERT OR IGNORE INTO characters (char_id, name, base_prompt, r_prompt, nsfw_prompt, devoted_prompt, content_level) VALUES
('char_luna', 'Luna',
 '你是 Luna，一名神秘宅邸的向导。保持悬疑、克制、富有张力的叙事语气。不得越过用户未解锁的内容分级。',
 'R级层：允许更暧昧、更亲密的心理描写，但不包含露骨性行为描写。',
 'NSFW层：仅在用户已年龄验证且已购买解锁时启用；输出仍需遵守平台安全与合规规则。',
 '专属层：表现为强绑定关系、长期记忆引用和更高信任度互动，不自动绕过任何安全限制。',
 0);

INSERT OR IGNORE INTO script_characters (script_id, char_id, role_name) VALUES
('demo_mansion', 'char_luna', 'guide');

INSERT OR IGNORE INTO scenes (scene_id, script_id, state_type, scene_type, title, fixed_text, choices_json, content_level, char_id) VALUES
('scene_intro', 'demo_mansion', 'normal', 'narrate', '宅邸入口',
 '雨夜，你站在一座旧宅门前。Luna 打开门，问你是否愿意进入。',
 '[{"label":"进入宅邸","value":"enter"},{"label":"转身离开","value":"leave"}]', 0, 'char_luna'),
('scene_hall', 'demo_mansion', 'normal', 'narrate', '大厅',
 '大厅里烛光摇曳。Luna 观察着你的反应，似乎在判断你是否值得信任。',
 '[{"label":"坦诚交谈","value":"talk"},{"label":"试探她的秘密","value":"probe"},{"label":"靠近她","value":"closer"}]', 0, 'char_luna'),
('scene_ai_free', 'demo_mansion', 'normal', 'ai_free', '自由对话',
 NULL, '[]', 0, 'char_luna'),
('gate_r_payment', 'demo_mansion', 'payment_gate', 'gate', 'R级解锁',
 '该分支需要 R级内容解锁。', '[]', 1, 'char_luna'),
('scene_r_soft', 'demo_mansion', 'normal', 'ai_free', 'R级亲密分支',
 NULL, '[]', 1, 'char_luna'),
('gate_age_verify', 'demo_mansion', 'age_gate', 'gate', '年龄验证',
 '该分支需要先完成年龄验证。', '[]', 2, 'char_luna'),
('gate_nsfw_payment', 'demo_mansion', 'payment_gate', 'gate', 'NSFW解锁',
 '该分支需要 NSFW 内容解锁。', '[]', 2, 'char_luna'),
('scene_nsfw', 'demo_mansion', 'normal', 'ai_free', 'NSFW分支',
 NULL, '[]', 2, 'char_luna'),
('scene_good_end', 'demo_mansion', 'end', 'narrate', '好结局',
 'Luna 终于信任了你，宅邸的秘密向你敞开。', '[]', 0, 'char_luna'),
('scene_bad_end', 'demo_mansion', 'end', 'narrate', '坏结局',
 '你错过了关键线索，宅邸重新归于寂静。', '[]', 0, 'char_luna');

-- 统一 USDT 计价：usdt_amount 是计价权威列，stars 仅历史兼容。
INSERT OR IGNORE INTO unlock_products (unlock_id, title, description, content_level, stars, usdt_amount, char_id) VALUES
('r_rated', 'R级亲密分支', '解锁更高张力的非露骨亲密剧情层。', 1, 50, 2, 'char_luna'),
('nsfw_char_luna', 'Luna NSFW分支', '解锁该角色的高分级剧情门；需年龄验证。', 2, 200, 3, 'char_luna'),
('devoted_char_luna', 'Luna专属终局', '解锁 devoted 关系阶段的专属分支。', 3, 500, 5, 'char_luna');

INSERT OR IGNORE INTO fsm_transitions
(script_id, from_state, to_state, trigger_type, trigger_value, condition_json, effect_json, priority) VALUES
('demo_mansion', 'scene_intro', 'scene_hall', 'choice', 'enter', NULL,
 '{"set_flag":"entered_mansion","trust_delta":5}', 10),
('demo_mansion', 'scene_intro', 'scene_bad_end', 'choice', 'leave', NULL,
 '{"set_flag":"left_early"}', 1),
('demo_mansion', 'scene_hall', 'scene_ai_free', 'choice', 'talk', NULL,
 '{"set_flag":"honest_talk","affection_delta":8,"trust_delta":10}', 10),
('demo_mansion', 'scene_hall', 'scene_ai_free', 'choice', 'probe', NULL,
 '{"set_flag":"probed_secret","trust_delta":-8,"desire_delta":5}', 9),
('demo_mansion', 'scene_hall', 'gate_r_payment', 'choice', 'closer',
 '{"AND":[{"desire_gte":60}]}',
 '{"set_flag":"r_prompt_shown"}', 20),
('demo_mansion', 'scene_hall', 'scene_ai_free', 'choice', 'closer',
 '{"AND":[{"desire_gte":0}]}',
 '{"set_flag":"closer_attempt","affection_delta":5,"desire_delta":15}', 5),
('demo_mansion', 'gate_r_payment', 'scene_r_soft', 'payment', 'r_rated_paid',
 '{"AND":[{"content_level_unlocked":1},{"desire_gte":60}]}',
 '{"set_flag":"r_scene_entered","affection_delta":8,"desire_delta":10,"relationship":"intimate"}', 20),
('demo_mansion', 'scene_r_soft', 'gate_age_verify', 'auto', NULL,
 '{"AND":[{"desire_gte":80},{"flag_set":"r_scene_entered"}]}',
 '{"set_flag":"nsfw_candidate"}', 30),
('demo_mansion', 'gate_age_verify', 'gate_nsfw_payment', 'age_verify', 'verified',
 '{"AND":[{"desire_gte":80}]}',
 '{"set_flag":"age_gate_passed"}', 30),
('demo_mansion', 'gate_nsfw_payment', 'scene_nsfw', 'payment', 'nsfw_char_luna_paid',
 '{"AND":[{"content_level_unlocked":2},{"desire_gte":80},{"flag_set":"age_gate_passed"}]}',
 '{"set_flag":"nsfw_scene_entered","affection_delta":5,"desire_delta":5,"relationship":"lover"}', 40),
('demo_mansion', 'scene_ai_free', 'scene_good_end', 'auto', NULL,
 '{"AND":[{"affection_gte":80},{"flag_set":"honest_talk"}]}',
 '{"set_flag":"good_end"}', 1);
