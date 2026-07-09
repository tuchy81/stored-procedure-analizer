"""단계별 로그 유틸."""
from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from rich.logging import RichHandler


def setup_stage_logger(stage: str, logs_dir: Path) -> logging.Logger:
    logs_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    logfile = logs_dir / f"{stage}_run_{ts}.log"

    logger = logging.getLogger(f"sp_assessor.{stage}")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    fh = logging.FileHandler(logfile, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(fh)

    logger.addHandler(RichHandler(rich_tracebacks=True, show_path=False))
    return logger
