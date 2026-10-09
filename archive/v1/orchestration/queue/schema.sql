-- orchestration SQLite 队列 schema (2026-09-22 ST-2 重构)
-- 由 SQLiteQueue._init_schema() 在每个新 db 文件上幂等执行。
-- 契约: docs/设计方案/pipeline-contracts.md §2.2
--
-- 新架构 ``simulation server → gdr → etl``:
--   - 单 task 走完 simulate → gdr → etl → done 全流程; phase 字段记录当前位置
--   - 不再有 batches / run_tasks / qf 阶段; 阶段间产物路径 (4 视图) 直接挂在 task 行
--   - 子进程按 multiprocessing.Pool 单 task 调度, 不再用 *_processing 中间态抢锁

CREATE TABLE IF NOT EXISTS tasks (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id           TEXT NOT NULL UNIQUE,
    run_id            TEXT,
    session_id        TEXT,
    phase             TEXT NOT NULL CHECK(phase IN
                          ('pending','simulate','gdr','etl','done','dead','audited')),
    attempts_simulate INTEGER NOT NULL DEFAULT 0,
    attempts_gdr      INTEGER NOT NULL DEFAULT 0,
    attempts_etl      INTEGER NOT NULL DEFAULT 0,
    src_path          TEXT,
    gdr_refined_path  TEXT,
    etl_messages_path TEXT,
    etl_openai_path   TEXT,
    etl_qwenjina_path TEXT,
    etl_meta_path     TEXT,
    error_msg         TEXT,
    started_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE INDEX IF NOT EXISTS idx_tasks_phase ON tasks(phase);
