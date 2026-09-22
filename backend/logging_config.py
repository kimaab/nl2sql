import logging
import logging.handlers
import sys
from pathlib import Path

LOG_DIR = Path(__file__).resolve().parent / "logs"


def configure_logging() -> None:
    """로깅 설정"""
    LOG_DIR.mkdir(exist_ok=True)
    root = logging.getLogger("nl2sql")
    root.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")

    # 콘솔 핸들러
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    root.addHandler(console)

    # 파일 핸들러
    file_handler = logging.handlers.RotatingFileHandler(
        LOG_DIR / "nl2sql.log",
        maxBytes=5_000_000,
        backupCount=3,
        encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)
