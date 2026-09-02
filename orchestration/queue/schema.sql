-- orchestration SQLite 队列 schema
-- 由 SQLiteQueue._init_schema() 在每个新 db 文件上幂等执行。
-- 字段语义详见 docs/orchestration-design.md §5。

CREATE TABLE IF NOT EXISTS tasks (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  src_path        TEXT NOT NULL UNIQUE,
  run_id          TEXT NOT NULL,
  session_id      TEXT,
  batch_id        INTEGER NOT NULL,
  state           TEXT NOT NULL,
  attempts_qf     INTEGER NOT NULL DEFAULT 0,
  attempts_gdr    INTEGER NOT NULL DEFAULT 0,
  qf_output_path  TEXT,
  gdr_output_path TEXT,
  error_msg       TEXT,
  locked_by       TEXT,
  locked_at       TEXT,
  created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  updated_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
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
  qf_started_at       TEXT,
  qf_done_at          TEXT,
  gdr_started_at      TEXT,
  gdr_done_at         TEXT,
  qf_count            INTEGER NOT NULL DEFAULT 0,
  gdr_count           INTEGER NOT NULL DEFAULT 0,
  dead_count          INTEGER NOT NULL DEFAULT 0,
  status              TEXT
);

CREATE INDEX IF NOT EXISTS idx_batches_status ON batches(status);

-- run_id → task_id 映射: producer 在 simulate 完成时写入 (它同时知道两者),
-- 供 qf/gdr worker 给产物文件名加 task_id 前缀 (可追溯性, 免去 join catalog)。
CREATE TABLE IF NOT EXISTS run_tasks (
  run_id     TEXT PRIMARY KEY,
  task_id    TEXT NOT NULL,
  batch_id   INTEGER,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);