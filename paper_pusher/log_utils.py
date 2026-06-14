"""日志工具 - 同时输出到控制台和文件"""

import logging
import sys
import traceback
from datetime import datetime
from pathlib import Path


_LOG_DIR = Path("logs")
_LOGGERS = {}


def get_logger(name: str = "paper_pusher") -> logging.Logger:
    if name in _LOGGERS:
        return _LOGGERS[name]

    _LOG_DIR.mkdir(exist_ok=True)

    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)

    if not logger.handlers:
        console = logging.StreamHandler(sys.stdout)
        console.setLevel(logging.INFO)
        console.setFormatter(logging.Formatter("%(message)s"))

        log_file = _LOG_DIR / f"paper_pusher_{datetime.now():%Y%m%d_%H%M%S}.log"
        file_h = logging.FileHandler(log_file, encoding="utf-8")
        file_h.setLevel(logging.DEBUG)
        file_h.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
        )

        logger.addHandler(console)
        logger.addHandler(file_h)

        def _log_exception(exc_type, exc_value, exc_tb):
            if issubclass(exc_type, KeyboardInterrupt):
                sys.__excepthook__(exc_type, exc_value, exc_tb)
                return
            tb_str = ''.join(traceback.format_exception(exc_type, exc_value, exc_tb))
            logger.error(f"未捕获异常:\n{tb_str}")

        sys.excepthook = _log_exception

    _LOGGERS[name] = logger
    return logger
