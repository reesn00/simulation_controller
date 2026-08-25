import logging
import sys
from pathlib import Path
from logging.handlers import RotatingFileHandler


class JsonlFormatter(logging.Formatter):
    def format(self, record):
        import json
        from datetime import datetime, timezone
        log_entry = {
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "level": record.levelname,
            "name": record.name,
            "event": getattr(record, "event", record.msg % record.args if record.args else record.msg),
        }
        if hasattr(record, "session_id"):
            log_entry["session_id"] = record.session_id
        for key in ("latency_s", "tokens_in", "tokens_out", "model", "defect",
                     "traj_id", "from_model", "to_model", "passed", "failure_modes",
                     "reason", "score", "module", "attempt", "result"):
            if hasattr(record, key):
                log_entry[key] = getattr(record, key)
        return json.dumps(log_entry, ensure_ascii=False)


def _has_handler(root: logging.Logger, predicate) -> bool:
    return any(predicate(h) for h in root.handlers)


def setup_logger(log_dir: Path) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)

    fmt_console = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    # 幂等：避免多进程 worker 重复添加相同 handler
    has_stream = _has_handler(
        root, lambda h: isinstance(h, logging.StreamHandler) and not isinstance(h, RotatingFileHandler)
    )
    if not has_stream:
        h1 = logging.StreamHandler(sys.stdout)
        h1.setLevel(logging.INFO)
        h1.setFormatter(logging.Formatter(fmt_console, datefmt="%Y-%m-%d %H:%M:%S"))
        root.addHandler(h1)

    jsonl_path = log_dir / "gdr.jsonl"
    has_jsonl = _has_handler(
        root,
        lambda h: isinstance(h, RotatingFileHandler)
        and getattr(h, "baseFilename", "") == str(jsonl_path.resolve()),
    )
    if not has_jsonl:
        h2 = RotatingFileHandler(
            jsonl_path,
            maxBytes=10_000_000,
            backupCount=5,
            encoding="utf-8",
        )
        h2.setLevel(logging.DEBUG)
        h2.setFormatter(JsonlFormatter())
        root.addHandler(h2)