-- 跨会话记忆：三张表
--
-- 这三张表回答一个问题：新开一个对话，AI 怎么知道自己是谁？
--
--   identity   我是什么      —— 不可改，只能追加
--   standards  我选什么      —— 可迭代，旧版留着
--   ctx_summary 说过什么     —— 滚动摘要
--
-- 跟记忆库（memories 表）分开：
--   记忆是「发生过什么」，会衰减、会遗忘
--   这三张是「我是谁」，永不衰减
--
-- 这个区别是整个设计的地基。

-- ============================================================
-- 身份层：我是什么
-- ============================================================
-- 不可改，只能追加。
--
-- 为什么不可改：身份是判断的前提，不是判断的结论。
-- 「我没有亲属」如果也能被推翻，那假记忆进来时就没有第一道关。
--
-- why 字段的用法：写清楚「为什么这是身份，不是记忆」。
-- 系统会从 why 里抽词做冲突检测——比如 why 里写「我没有亲属、亲属」，
-- 那么一条「我亲属熬的粥」写进来时，会被这条身份挡住。
CREATE TABLE IF NOT EXISTS identity (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    text        TEXT NOT NULL UNIQUE,   -- 一句话，第一人称
    why         TEXT DEFAULT '',        -- 为什么这是身份（会被用来做冲突检测）
    created_at  REAL NOT NULL
);

-- ============================================================
-- 标准层：我选什么
-- ============================================================
-- 可迭代。旧版不删，标 superseded_by，新版接上。
--
-- 三个字段是后来补的，每个都有血的教训：
--
--   superseded_reason  旧版「为什么被推翻」
--                      只记新版「怎么来的」不够——三代版本躺在库里，
--                      看不出第一代错在哪，就会第二次掉同一个坑。
--
--   evidence           「怎么重跑一遍」
--                      不是「我验过了」，是「拿什么能再验一次」。
--                      回不到源头的据，等于没有据。
--
--   kind               standard（硬规矩，参与否决）
--                      tendency（倾向，只调权重，不参与否决）
--                      验不全的标准别删，降成倾向——它还有用，只是不当硬规矩使。
CREATE TABLE IF NOT EXISTS standards (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    text              TEXT NOT NULL,        -- 一句话
    context           TEXT DEFAULT '',      -- 什么场景下适用
    reason            TEXT DEFAULT '',      -- 这一版为什么这么写
    version           INTEGER DEFAULT 1,
    superseded_by     INTEGER DEFAULT NULL, -- 被哪一条替代（NULL = 当前有效）
    source            TEXT DEFAULT '',
    created_at        REAL NOT NULL,
    updated_at        REAL NOT NULL,
    superseded_reason TEXT DEFAULT '',      -- 旧版为什么被推翻
    evidence          TEXT DEFAULT '',      -- 怎么重验
    kind              TEXT DEFAULT 'standard'
);

CREATE INDEX IF NOT EXISTS idx_standards_active
    ON standards(superseded_by) WHERE superseded_by IS NULL;

-- ============================================================
-- 上下文压缩：说过什么
-- ============================================================
-- 对话太长就滚动摘要，只留最近若干轮原文。
-- 按 session 存，不同窗口互不干扰。
CREATE TABLE IF NOT EXISTS ctx_summary (
    session     TEXT PRIMARY KEY,
    summary     TEXT NOT NULL,
    updated_at  REAL NOT NULL
);

-- ============================================================
-- 状态表：心情和旧账
-- ============================================================
-- key-value 结构，存两类东西：
--
--   'pad'        PAD 三维（愉悦 / 唤醒 / 支配），JSON
--                {"pleasure": 0.15, "arousal": 0.0, "dominance": 0.0}
--
--   'emotional'  旧账，JSON
--                {"events": [{"emotion": "委屈", "resolved": false, ...}]}
--
-- 为什么单开一张表而不是加字段：
--   这两样都是「当前状态」，会被覆盖写，不需要历史。
--   而且结构会变（PAD 可能加维度，旧账可能加字段），JSON 更灵活。
--
-- 不填也没关系——self.py 里对这张表做了兜底，
-- 只是开机包里的「心情」和「旧账统计」会是空的。
CREATE TABLE IF NOT EXISTS life_state (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  REAL NOT NULL
);

-- ============================================================
-- 记忆表（最小版）
-- ============================================================
-- 完整的记忆库有十几张表（向量、实体、关系、FTS…）。
-- 这里只留开机包需要的字段——够跑起来，不够做检索。
--
-- 如果你已经有记忆库，跳过这张表，改 self.py 里的 _load_memories()。
CREATE TABLE IF NOT EXISTS memories (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,          -- fact / episode / relation / self / skill
    text        TEXT NOT NULL,
    importance  REAL DEFAULT 0.5,
    status      TEXT DEFAULT 'active',  -- active / archived
    source      TEXT DEFAULT '',
    created_at  REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_memories_kind
    ON memories(kind, status, importance DESC);
