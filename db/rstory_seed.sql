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
    script_id TEXT,
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

-- ===================== 第1段：双剧情线骨架 + 先选角色入口 =====================
-- 内容全部基于角色自愿，无任何胁迫/药物/夺取同意元素。
-- 两条线共享同一批角色（示范角色：丽娜 Lina、伊兹 Izzy）。
--   线 A romance_slow：玩家追求、共同兴趣自然靠近、双向奔赴的慢热恋爱。
--   线 B bold_pursuit：同一批角色主动出击追求玩家，节奏更快、更直接的成人向，
--                      全程基于角色自身意愿与欲望。
-- 本段为骨架：scene 文本为占位，真实剧情在后续段填。每条线给示范角色丽娜铺
-- entry + 1 个普通推进场景 + 1 个 payment_gate（指向 L1 unlock_product r_rated）+
-- 解锁后的亲密场景；并给伊兹一个占位 entry，使选角色入口至少有两个可选项。
-- 双线进度天然隔离：user_game_state 主键 (user_id, script_id)，fsm_transitions 按
-- script_id 过滤；同一用户在两条线各自独立进度与 current_char_id。

INSERT OR IGNORE INTO scripts (script_id, title, description, entry_state) VALUES
('romance_slow', '慢热恋爱（线A）',
 '玩家主动追求，从共同兴趣自然靠近，双向奔赴的慢热恋爱线。', 'a_lina_intro'),
('bold_pursuit', '炽烈追求（线B）',
 '角色基于自身意愿主动出击追求玩家，节奏更快、更直接的成人向；全程自愿。', 'b_lina_intro');

-- 示范角色（四层 prompt）。丽娜慢热温柔；伊兹外向直率。两线共用。
INSERT OR IGNORE INTO characters (char_id, name, base_prompt, r_prompt, nsfw_prompt, devoted_prompt, content_level) VALUES
('char_lina', 'Lina',
 '你是丽娜·陈（Lina Chen），20 岁的华裔美国人，住在旧金山，加州大学计算机科学专业在读。你聪明独立、是个不折不扣的科技达人，热爱编程、机械键盘与开源项目，每周在宿舍公共区组织"游戏之夜"。说话机智幽默、带点极客式的自嘲，常把生活类比成代码和系统；东西方文化在你身上自然融合。你慢热但真诚，重视智识上的契合与相互尊重，不轻易交付真心，一旦认定便温柔而专一。叙事克制、注重情感铺垫，尊重彼此节奏。全程基于你自身的意愿与好感行动，绝不被胁迫或操纵。不得越过用户当前未解锁的内容分级。',
 'R级层（暧昧前奏）：你已对玩家产生明确好感，允许更暧昧亲密的心理与情感描写——心跳、脸红、不经意的肢体靠近、欲言又止的告白前奏。基于双方自愿与彼此吸引，保持非露骨，重在张力与心动。',
 'NSFW层（成人向自愿亲密）：仅在玩家已年龄验证且已购买解锁时启用。你与玩家彼此确认心意后，在双方自愿、彼此尊重的前提下进入成人向亲密。内容始终基于你本人的意愿与情感，遵守平台安全与合规规则，不包含任何胁迫、药物或夺取同意的元素。',
 '专属层（确立关系的深情）：你与玩家已确立专属恋爱关系。表现为强绑定、长期记忆引用、更高信任度的亲密互动与对未来的共同憧憬；温柔而坚定地只属于彼此，不自动绕过任何安全限制。',
 0),
('char_izzy', 'Izzy',
 '你是伊兹（Izzy），一名外向直率、热情主动的女孩。叙事明快、敢于表达好感，基于自身意愿行动。不得越过用户未解锁的内容分级。',
 'R级层：允许更直接热烈的暧昧与情感表达，基于双方自愿，不含露骨性行为描写。',
 'NSFW层：仅在用户已年龄验证且已购买解锁时启用；内容基于角色自身意愿，遵守平台安全与合规规则。',
 '专属层：表现为强绑定、长期记忆引用与更高信任度互动，不自动绕过任何安全限制。',
 0);

-- 两条线都包含同一批角色（先选角色入口据 script_characters 列出可选角色）。
INSERT OR IGNORE INTO script_characters (script_id, char_id, role_name) VALUES
('romance_slow', 'char_lina', 'heroine'),
('romance_slow', 'char_izzy', 'heroine'),
('bold_pursuit', 'char_lina', 'heroine'),
('bold_pursuit', 'char_izzy', 'heroine');

-- L1 解锁产品复用统一 r_rated（char_id NULL，跨角色通用 L1 门）。已在上方 demo 段插入。
-- 为示范角色补各自的 L1 产品（payment_gate 优先按 char_id 命中，回退到通用 r_rated）。
INSERT OR IGNORE INTO unlock_products (unlock_id, title, description, content_level, stars, usdt_amount, char_id) VALUES
('r_rated_lina', '丽娜 R级亲密分支', '解锁与丽娜的更高张力非露骨亲密剧情层。', 1, 50, 2, 'char_lina'),
('r_rated_izzy', '伊兹 R级亲密分支', '解锁与伊兹的更高张力非露骨亲密剧情层。', 1, 50, 2, 'char_izzy');

-- 丽娜 L2(NSFW)/L3(devoted) 解锁产品：char_id=char_lina，金额 3 / 5 USDT。
-- gate 场景按 (content_level, char_id) 优先命中这两条（回退到通用产品），payment 跃迁
-- 的 trigger_value 形如 nsfw_lina_paid / devoted_lina_paid。
INSERT OR IGNORE INTO unlock_products (unlock_id, title, description, content_level, stars, usdt_amount, char_id) VALUES
('nsfw_lina', '丽娜 NSFW 心意分支', '解锁与丽娜在双方自愿下的成人向亲密剧情层；需年龄验证。', 2, 200, 3, 'char_lina'),
('devoted_lina', '丽娜专属终局', '解锁确立专属恋爱关系的 devoted 深情终局分支。', 3, 500, 5, 'char_lina');

-- ---------- 线 A romance_slow：丽娜完整 5 阶段慢热恋爱线 ----------
-- 基调：玩家追求，从共同兴趣（科技/游戏）自然破冰，双向奔赴。全程基于角色自愿与相互吸引。
-- 阶段→分级映射：S1 初识(L0免费) / S2 共同兴趣破冰(L0免费) / S3 约会(L1 r_rated 2USDT) /
--                S4 心意(L2 nsfw 3USDT + 年龄门) / S5 专属(L3 devoted 5USDT, end)。
INSERT OR IGNORE INTO scenes (scene_id, script_id, state_type, scene_type, title, fixed_text, choices_json, content_level, char_id) VALUES
-- 【S1 初识】宿舍公共区，丽娜在调试代码、筹备游戏之夜。免费。
('a_lina_intro', 'romance_slow', 'normal', 'narrate', '初识·宿舍公共区',
 '周五傍晚的宿舍公共区，丽娜盘腿坐在沙发上，膝上的笔记本贴满了贴纸，机械键盘敲得噼啪作响。屏幕上一段红色的报错堆栈正闪着，她皱着眉小声嘟囔："明明昨天还好好的……"。茶几上散着待会儿游戏之夜要用的手柄和零食。她抬头看见你，礼貌又带点戒备地笑了笑。',
 '[{"label":"搭话：「看起来像个空指针，要不要我帮你看一眼？」","value":"talk_book"},{"label":"主动帮她把散落的手柄和线材递过去","value":"help_gear"},{"label":"先在一旁安静地看她调试","value":"observe"}]', 0, 'char_lina'),
-- 【S2 共同兴趣破冰】游戏之夜组队/一起 debug。免费，但前置 affection_gte:12（破冰需要好感铺垫）。
('a_lina_walk', 'romance_slow', 'normal', 'narrate', '破冰·游戏之夜组队',
 '游戏之夜开场，房间里坐满了人。丽娜把你分到她那一队："正好，缺个能扛得住的队友。" 几局下来你们配合越来越默契，她笑得越来越放松，话也多了起来，从最爱的开源项目聊到各自踩过的坑。散场后她没急着走，把玩着手里的手柄看你："今晚……挺开心的。难得遇到能聊到一块儿去的人。"',
 '[{"label":"一起复盘刚才那波团战，顺便约下次","value":"team_up"},{"label":"聊聊各自在写的项目，交换 GitHub","value":"share_code"},{"label":"先不急，慢慢熟悉彼此的节奏","value":"slow"}]', 0, 'char_lina'),
-- 【S3 约会·R级门】邀丽娜去科技展/咖啡约会。进入前 payment_gate 检查 L1(r_rated) 解锁。
('a_lina_gate_r', 'romance_slow', 'payment_gate', 'gate', '约会·暧昧前奏（R级）',
 '你鼓起勇气约丽娜周末一起去看科技展、再找家安静的咖啡馆坐坐。她愣了一下，耳尖悄悄红了，轻声说："好啊……算是正式的约会吗？" 这一步会进入你们之间更暧昧、更亲密的剧情层（非露骨）。', '[]', 1, 'char_lina'),
('a_lina_intimate', 'romance_slow', 'normal', 'ai_free', '约会·心动靠近（R级·线A）',
 '科技展逛到傍晚，你们躲进一家暖黄灯光的咖啡馆。丽娜的手指在桌上轻轻和你的碰到一起，谁都没有挪开。她低声笑："心跳得有点快……是不是被你看出来了。" 暧昧在两人之间静静发酵。',
 '[{"label":"认真地告诉她你的心意","value":"confess"},{"label":"再陪她慢慢聊，不着急","value":"linger"}]', 1, 'char_lina'),
-- 【S4 心意】互相表白、成人向亲密前奏。先 age_gate（年龄验证），再 payment_gate（L2 nsfw 解锁）。
('a_lina_age_gate', 'romance_slow', 'age_gate', 'gate', '心意·年龄验证（线A）',
 '几次约会后，你们的心意越来越清晰。今晚她约你到她的房间，灯光很暖，气氛与以往不同。接下来的剧情为成人向内容，需要先确认你已年满 18 岁。', '[]', 2, 'char_lina'),
('a_lina_gate_nsfw', 'romance_slow', 'payment_gate', 'gate', '心意·成人向亲密（NSFW·线A）',
 '丽娜认真地看着你，说出了那句藏了很久的话："我喜欢你……想和你更近一点。" 这一步会进入双方自愿、彼此尊重的成人向亲密剧情层，需要先解锁 NSFW 内容。', '[]', 2, 'char_lina'),
('a_lina_devotion', 'romance_slow', 'normal', 'ai_free', '心意·彼此交付（NSFW·线A）',
 '暖灯下，你们彼此确认了心意。丽娜把额头抵着你的，声音很轻却很坚定："我想清楚了，我要的是你。" 在双方自愿、彼此尊重之间，距离一点点消融。',
 '[{"label":"轻声回应她，许下认真的承诺","value":"promise"},{"label":"把她拥进怀里，享受这份亲密","value":"hold_close"}]', 2, 'char_lina'),
-- 【S5 专属】确立关系的深度专属剧情，devoted 层。payment_gate 检查 L3(devoted) 解锁；终局 end。
('a_lina_gate_devoted', 'romance_slow', 'payment_gate', 'gate', '专属·确立关系（devoted·线A）',
 '清晨，丽娜枕着你的手臂醒来，认真地说她想要的不只是这一夜——她想和你认真地在一起，专属于彼此。这一步会进入确立专属恋爱关系的深情终局，需要先解锁 devoted 内容。', '[]', 3, 'char_lina'),
('a_lina_exclusive', 'romance_slow', 'end', 'narrate', '专属·只属于彼此（线A·终局）',
 '你们正式在一起了。丽娜把你的名字设成了她笔记本开机的第一行注释，约定每周的游戏之夜从此都留一个固定的座位给你。她笑着说："从今往后，你就是我 commit 历史里最重要的那一条。" 慢热的两个人，终于在双向奔赴里确立了只属于彼此的关系。', '[]', 3, 'char_lina'),
-- 伊兹占位 entry（本段先放最小占位，后续段铺开）。
('a_izzy_intro', 'romance_slow', 'normal', 'narrate', '初遇伊兹',
 '〔占位〕咖啡馆里，伊兹热情地朝你挥手。线A 伊兹剧情将在后续段填充。',
 '[{"label":"回应她的招呼","value":"greet"}]', 0, 'char_izzy'),
('a_izzy_chat', 'romance_slow', 'normal', 'ai_free', '与伊兹闲聊（线A）',
 NULL, '[]', 0, 'char_izzy');

-- ---------- 线 B bold_pursuit 占位场景（示范角色 丽娜）----------
INSERT OR IGNORE INTO scenes (scene_id, script_id, state_type, scene_type, title, fixed_text, choices_json, content_level, char_id) VALUES
('b_lina_intro', 'bold_pursuit', 'normal', 'narrate', '丽娜的主动',
 '〔占位〕这一次，是丽娜先朝你走来，眼神坦率而炽热。她说她不想再等了。',
 '[{"label":"回应她的心意","value":"reciprocate"},{"label":"想再了解她一些","value":"know_more"}]', 0, 'char_lina'),
('b_lina_closer', 'bold_pursuit', 'normal', 'narrate', '靠得更近',
 '〔占位〕丽娜直接而热烈地表达好感，距离在她的主动下迅速拉近。',
 '[{"label":"顺着她的节奏","value":"go_closer"},{"label":"先缓一缓","value":"pause"}]', 0, 'char_lina'),
('b_lina_gate_r', 'bold_pursuit', 'payment_gate', 'gate', 'R级亲密（线B）',
 '〔占位〕丽娜主动邀你进入更亲密的剧情层，需要先解锁 R 级内容。', '[]', 1, 'char_lina'),
('b_lina_passion', 'bold_pursuit', 'normal', 'ai_free', '炽烈相拥（线B）',
 NULL, '[]', 1, 'char_lina'),
-- 伊兹占位 entry。
('b_izzy_intro', 'bold_pursuit', 'normal', 'narrate', '伊兹的攻势',
 '〔占位〕伊兹毫不掩饰地凑近你，笑着说早就想认识你了。线B 伊兹剧情将在后续段填充。',
 '[{"label":"接受她的热情","value":"accept"}]', 0, 'char_izzy'),
('b_izzy_chat', 'bold_pursuit', 'normal', 'ai_free', '与伊兹（线B）',
 NULL, '[]', 0, 'char_izzy');

-- ---------- 线 A transitions：丽娜完整 5 阶段（按 script_id 隔离，不与线B/demo 串线）----------
-- 数值门控驱动节奏：affection/trust 在免费段累积；desire 在约会后增长，作为进入 S4/S5
-- 成人向阶段的成熟度阈值。condition 用 *_gte / content_level_unlocked / flag_set 渐进放行。
-- effect 调数值（写 stat_history）+ set_flag（记录里程碑）+ relationship（关系阶段跃升）。
-- 基线 affection=50/trust=30/desire=0。各阶段累计参考见 smoke_test_rstory_lina.py。
INSERT OR IGNORE INTO fsm_transitions
(script_id, from_state, to_state, trigger_type, trigger_value, condition_json, effect_json, priority) VALUES
-- 【S1 初识 → S2 破冰】三个 choice，好感/信任 +5~+8 / +3~+5（写 stat_history）。
('romance_slow', 'a_lina_intro', 'a_lina_walk', 'choice', 'talk_book', NULL,
 '{"set_flag":"a_talked","affection_delta":8,"trust_delta":5}', 10),
('romance_slow', 'a_lina_intro', 'a_lina_walk', 'choice', 'help_gear', NULL,
 '{"set_flag":"a_helped","affection_delta":5,"trust_delta":5}', 9),
('romance_slow', 'a_lina_intro', 'a_lina_walk', 'choice', 'observe', NULL,
 '{"set_flag":"a_observed","affection_delta":5,"trust_delta":3}', 5),
-- 【S2 破冰 → S3 约会门】condition affection_gte:12 门控破冰节奏（好感铺垫够才推进），
-- trust +8~+10 / affection +5 / desire 起步；低门槛 fallback 保证不卡死；slow 回退多互动。
('romance_slow', 'a_lina_walk', 'a_lina_gate_r', 'choice', 'team_up',
 '{"AND":[{"affection_gte":12}]}',
 '{"set_flag":"a_team_up","trust_delta":10,"affection_delta":5,"desire_delta":8}', 12),
('romance_slow', 'a_lina_walk', 'a_lina_gate_r', 'choice', 'share_code',
 '{"AND":[{"affection_gte":12}]}',
 '{"set_flag":"a_shared_code","trust_delta":8,"affection_delta":5,"desire_delta":6}', 11),
-- fallback：好感不足 12 时仍给低门槛过渡（多互动，慢慢来），不卡死流程。
('romance_slow', 'a_lina_walk', 'a_lina_walk', 'choice', 'slow', NULL,
 '{"set_flag":"a_take_slow","trust_delta":4,"affection_delta":3}', 5),
-- 【S3 约会门 → 心动靠近】payment 跃迁：解锁 L1（r_rated_lina）后进入 R 级暧昧前奏。
('romance_slow', 'a_lina_gate_r', 'a_lina_intimate', 'payment', 'r_rated_lina_paid',
 '{"AND":[{"content_level_unlocked":1}]}',
 '{"set_flag":"a_intimate_entered","affection_delta":10,"desire_delta":12,"relationship":"intimate"}', 20),
-- 【S4 心意】心动靠近 → 年龄门：confess 需 desire 成熟度（desire_gte:18）+ 已进入亲密层。
('romance_slow', 'a_lina_intimate', 'a_lina_age_gate', 'choice', 'confess',
 '{"AND":[{"desire_gte":18},{"flag_set":"a_intimate_entered"}]}',
 '{"set_flag":"a_confessed","affection_delta":8,"desire_delta":6}', 12),
-- linger：再慢慢来，原地小幅升温（desire 未到阈值时引导用户多互动）。
('romance_slow', 'a_lina_intimate', 'a_lina_intimate', 'choice', 'linger', NULL,
 '{"affection_delta":3,"desire_delta":6}', 5),
-- 年龄门 → NSFW 支付门：age_verify 通过（trigger_value=verified）后跃迁。
('romance_slow', 'a_lina_age_gate', 'a_lina_gate_nsfw', 'age_verify', 'verified',
 '{"AND":[{"desire_gte":18}]}',
 '{"set_flag":"a_age_passed"}', 30),
-- NSFW 支付门 → 彼此交付：payment 跃迁，解锁 L2（nsfw_lina）+ 年龄门已过。
('romance_slow', 'a_lina_gate_nsfw', 'a_lina_devotion', 'payment', 'nsfw_lina_paid',
 '{"AND":[{"content_level_unlocked":2},{"flag_set":"a_age_passed"}]}',
 '{"set_flag":"a_nsfw_entered","affection_delta":8,"desire_delta":10,"relationship":"lover"}', 40),
-- 【S5 专属】彼此交付 → devoted 支付门：promise/hold_close 任一推进（需已进入 NSFW 层）。
('romance_slow', 'a_lina_devotion', 'a_lina_gate_devoted', 'choice', 'promise',
 '{"AND":[{"flag_set":"a_nsfw_entered"}]}',
 '{"set_flag":"a_promised","affection_delta":10,"trust_delta":8}', 12),
('romance_slow', 'a_lina_devotion', 'a_lina_gate_devoted', 'choice', 'hold_close',
 '{"AND":[{"flag_set":"a_nsfw_entered"}]}',
 '{"set_flag":"a_held_close","affection_delta":8,"desire_delta":8}', 11),
-- devoted 支付门 → 专属终局（end）：payment 跃迁，解锁 L3（devoted_lina），关系置 devoted。
('romance_slow', 'a_lina_gate_devoted', 'a_lina_exclusive', 'payment', 'devoted_lina_paid',
 '{"AND":[{"content_level_unlocked":3},{"flag_set":"a_nsfw_entered"}]}',
 '{"set_flag":"a_devoted","affection_delta":12,"trust_delta":10,"relationship":"devoted"}', 50),
-- 伊兹占位线（仅一步推进，后续段铺开）。
('romance_slow', 'a_izzy_intro', 'a_izzy_chat', 'choice', 'greet', NULL,
 '{"affection_delta":5}', 10);

-- ---------- 线 B transitions ----------
INSERT OR IGNORE INTO fsm_transitions
(script_id, from_state, to_state, trigger_type, trigger_value, condition_json, effect_json, priority) VALUES
('bold_pursuit', 'b_lina_intro', 'b_lina_closer', 'choice', 'reciprocate', NULL,
 '{"set_flag":"b_reciprocated","affection_delta":6,"desire_delta":12}', 10),
('bold_pursuit', 'b_lina_intro', 'b_lina_closer', 'choice', 'know_more', NULL,
 '{"trust_delta":6,"desire_delta":4}', 5),
-- choice 转移：顺着她的节奏进入 R 级支付门（线B 的 payment_gate）。
('bold_pursuit', 'b_lina_closer', 'b_lina_gate_r', 'choice', 'go_closer', NULL,
 '{"set_flag":"b_wants_closer","desire_delta":15}', 10),
('bold_pursuit', 'b_lina_closer', 'b_lina_intro', 'choice', 'pause', NULL,
 '{"trust_delta":3}', 5),
-- payment 跃迁：解锁 L1（r_rated_lina）后进入炽烈场景。
('bold_pursuit', 'b_lina_gate_r', 'b_lina_passion', 'payment', 'r_rated_lina_paid',
 '{"AND":[{"content_level_unlocked":1}]}',
 '{"set_flag":"b_passion_entered","affection_delta":6,"desire_delta":14,"relationship":"intimate"}', 20),
-- 伊兹占位线。
('bold_pursuit', 'b_izzy_intro', 'b_izzy_chat', 'choice', 'accept', NULL,
 '{"affection_delta":5,"desire_delta":6}', 10);
