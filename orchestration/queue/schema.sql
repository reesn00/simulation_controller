-- orchestration SQLite 队列 schema
-- 由 SQLiteQueue._init_schema() 在每个新 db 文件上幂等执行。
-- 字段语义详见 docs/orchestration-design.md §5。
--
-- 新架构 ``simulation server → gdr → etl``：
--   - gdr 阶段消费 trajectory（C1），写单 C2 refined Session（gdr_refined_path）
--   - etl 阶段消费 C2，写 4 视图（etl_*_path）
--   - qf 阶段已删除；attempts_qf / qf_output_path 不再存在

CREATE TABLE IF NOT EXISTS tasks (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  src_path          TEXT NOT NULL UNIQUE,
  run_id            TEXT NOT NULL,
  session_id        TEXT,
  batch_id          INTEGER NOT NULL,
  state             TEXT NOT NULL,
  attempts_gdr      INTEGER NOT NULL DEFAULT 0,
  attempts_etl      INTEGER NOT NULL DEFAULT 0,
  gdr_refined_path  TEXT,
  etl_messages_path TEXT,
  etl_openai_path   TEXT,
  etl_qwenjina_path TEXT,
  etl_meta_path     TEXT,
  error_msg         TEXT,
  locked_by         TEXT,
  locked_at         TEXT,
  created_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  updated_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_tasks_state ON tasks(state, batch_id);
CREATE INDEX IF NOT EXISTS idx_tasks_run   ON tasks(run_id);

CREATE TABLE IF NOT EXISTS batches (
  id                  INTEGER PRIMARY KEY AUTOINCREMENT,
  task_ids            TEXT NOT NULL,
  simulate_started_at TEXT,
  simulate_done_at    TEXT,
  -- 阶段级时间戳 (可观测性): 首次有 task 进入该阶段时写 *_started_at,
  -- 批内再无该阶段在途 task 时写 *_done_at。旧 db 由 _init_schema 幂等 ALTER 补列。
  gdr_started_at      TEXT,
  gdr_done_at         TEXT,
  etl_started_at      TEXT,
  etl_done_at         TEXT,
  gdr_count           INTEGER NOT NULL DEFAULT 0,
  etl_count           INTEGER NOT NULL DEFAULT 0,
  dead_count          INTEGER NOT NULL DEFAULT 0,
  status              TEXT
);

CREATE INDEX IF NOT EXISTS idx_batches_status ON batches(status);

-- run_id → task_id 映射: producer 在 simulate 完成时写入 (它同时知道两者),
-- 供 gdr/etl worker 给产物文件名加 task_id 前缀 (可追溯性, 免去 join catalog)。
CREATE TABLE IF NOT EXISTS run_tasks (
  run_id     TEXT PRIMARY KEY,
  task_id    TEXT NOT NULL,
  batch_id   INTEGER,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);