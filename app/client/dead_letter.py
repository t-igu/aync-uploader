# dead_letter.py
import json
from pathlib import Path
from datetime import datetime
from app.utils.config_util.config_loader import worker_config

DLQ_DIR = Path(worker_config.get("dlq_dir", "."))
DLQ_DIR.mkdir(exist_ok=True)


def write_dead_letter(request_id: str, payload: dict, reason: str, logger):
    """失敗したジョブを dead-letter queue に保存する"""
    path = DLQ_DIR / f"{request_id}.json"

    data = {
        "request_id": request_id,
        "reason": reason,
        "payload": payload,
        "timestamp": datetime.utcnow().isoformat(),
    }

    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    logger.error(
        event_message="dead_letter_written",
        data={"request_id": request_id, "reason": reason, "path": str(path)}
    )
