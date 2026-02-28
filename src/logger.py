"""
Logging setup for the options trading bot.
Configures rotating file handler + console handler.
Sensitive values (tokens, account IDs) are masked automatically.
"""

import logging
import logging.handlers
import re
import os
from typing import Dict


# Fields to mask in log output
_SENSITIVE_PATTERNS = [
    (r'(bot_token["\s:=]+)[^\s,}\n"]+', r'\1***'),
    (r'(chat_id["\s:=]+)[^\s,}\n"]+',   r'\1***'),
    (r'(acc_id["\s:=]+)\d+',             r'\1***'),
    (r'(account_id["\s:=]+)\d+',         r'\1***'),
    (r'(password["\s:=]+)[^\s,}\n"]+',   r'\1***'),
]


class SensitiveMaskFilter(logging.Filter):
    """Filters sensitive values out of all log records."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self._mask(str(record.msg))
        record.args = None   # prevent double-formatting issues
        return True

    def _mask(self, text: str) -> str:
        for pattern, replacement in _SENSITIVE_PATTERNS:
            text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
        return text


def setup_logger(config: Dict) -> logging.Logger:
    """
    Configure and return the root bot logger.

    Args:
        config: Dict with keys: level, file, max_bytes, backup_count

    Returns:
        Configured logger instance.
    """
    log_config  = config.get("logging", {})
    level_str   = log_config.get("level", "INFO").upper()
    log_file    = log_config.get("file", "logs/bot.log")
    max_bytes   = log_config.get("max_bytes", 10_485_760)   # 10 MB
    backup_count = log_config.get("backup_count", 5)

    level = getattr(logging, level_str, logging.INFO)

    # Ensure log directory exists
    os.makedirs(os.path.dirname(log_file), exist_ok=True)

    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)-30s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    mask_filter = SensitiveMaskFilter()

    # Rotating file handler
    file_handler = logging.handlers.RotatingFileHandler(
        filename=log_file,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    file_handler.addFilter(mask_filter)

    # Console handler
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    console_handler.addFilter(mask_filter)

    # Root bot logger
    logger = logging.getLogger("options_bot")
    logger.setLevel(level)
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    logger.propagate = False

    logger.info(f"Logger initialised | level={level_str} | file={log_file}")
    return logger


def get_logger(name: str) -> logging.Logger:
    """
    Get a child logger under the options_bot namespace.

    Args:
        name: Module name e.g. 'connectors.moomoo'

    Returns:
        Child logger that inherits root bot logger config.
    """
    return logging.getLogger(f"options_bot.{name}")
