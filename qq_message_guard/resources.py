from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sys
import traceback
from datetime import datetime, timedelta, timezone

from .config import GuardError


def redact(text):
    text = re.sub(r"(?i)\b(?:https?|wss?)://[^\s\"'<>]+", "[URL隐藏]", str(text))
    text = re.sub(r"(?i)\babk_[A-Za-z0-9_-]+", "[密钥隐藏]", text)
    text = re.sub(r"(?i)\bBearer\s+[^\s,;\"']+", "Bearer [隐藏]", text)
    return re.sub(
        r"(?i)((?:api_key|token|password|cookie|secret)[\"']?\s*[:=]\s*)"
        r"(?:\"[^\"]*\"|'[^']*'|[^\s,;}]+)",
        r"\1[隐藏]",
        text,
    )


def exception_info(exc):
    return {
        "type": type(exc).__name__,
        "message": redact(str(exc)),
        "frames": [
            {"file": f.filename, "line": f.lineno, "function": f.name}
            for f in traceback.extract_tb(exc.__traceback__)
        ],
    }


class InstanceLock:
    def __init__(self, path):
        self.file = path.open("a+b")
        try:
            if os.fstat(self.file.fileno()).st_size == 0:
                self.file.write(b"0")
                self.file.flush()
            self.file.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.file.close()
            raise GuardError("已有一个群消息审核实例在运行。") from exc

    def close(self):
        self.file.close()


class DailyHandler(logging.Handler):
    """Append-only daily segments; no renaming files that another process may read."""

    healthy = True

    def __init__(self, directory, prefix, max_bytes):
        super().__init__()
        self.directory, self.prefix, self.max_bytes = directory, prefix, max_bytes
        self.stream, self.day, self.sequence = None, "", 0

    def emit(self, record):
        try:
            day = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")
            text = self.format(record) + "\n"
            size = len(text.encode("utf-8"))
            if self.day != day:
                if self.stream:
                    self.stream.close()
                self.stream, self.day = None, day
                existing = sorted(self.directory.glob(f"{self.prefix}-{day}-*.jsonl"))
                self.sequence = int(existing[-1].stem.rsplit("-", 1)[1]) if existing else 1
            if self.stream and os.fstat(self.stream.fileno()).st_size + size > self.max_bytes:
                self.stream.close()
                self.stream = None
                self.sequence += 1
            if self.stream is None:
                path = self.directory / f"{self.prefix}-{day}-{self.sequence:05d}.jsonl"
                if path.exists() and path.stat().st_size + size > self.max_bytes:
                    self.sequence += 1
                    path = self.directory / f"{self.prefix}-{day}-{self.sequence:05d}.jsonl"
                self.stream = path.open("a", encoding="utf-8")
            self.stream.write(text)
            self.stream.flush()
        except Exception:
            self.handleError(record)

    def handleError(self, record):
        self.healthy = False
        # Host logger should receive a short failure without the sensitive record.
        try:
            logging.getLogger(__name__).error("QQ消息审核日志写入失败：%s", type(sys.exc_info()[1]).__name__)
        except Exception:
            pass

    def close(self):
        if self.stream:
            self.stream.close()
            self.stream = None
        super().close()


class Journal:
    def __init__(self, root, settings):
        self.root = root
        (root / "logs").mkdir(parents=True, exist_ok=True)
        self.handler = DailyHandler(root / "logs", "events", settings.log_mb * 1024 * 1024)
        self.harmful_handler = DailyHandler(root / "logs", "risk", settings.log_mb * 1024 * 1024)
        self.handler.setFormatter(logging.Formatter("%(message)s"))
        self.harmful_handler.setFormatter(logging.Formatter("%(message)s"))
        self.logger = logging.Logger(f"qq-message-guard-{id(self)}", logging.INFO)
        self.logger.addHandler(self.handler)
        self.harmful_logger = logging.Logger(f"qq-message-risk-{id(self)}", logging.INFO)
        self.harmful_logger.addHandler(self.harmful_handler)

    def record(self, event, *, harmful=False, **fields):
        if isinstance(fields.get("exception"), BaseException):
            fields["exception"] = exception_info(fields["exception"])

        def clean(value):
            if isinstance(value, str):
                return redact(value)
            if isinstance(value, dict):
                return {k: clean(v) for k, v in value.items()}
            if isinstance(value, (tuple, list)):
                return [clean(v) for v in value]
            return value

        target = self.harmful_logger if harmful else self.logger
        target.info(
            json.dumps(
                {
                    "at": datetime.now(timezone(timedelta(hours=8))).isoformat(),
                    "event": event,
                    **clean(fields),
                },
                ensure_ascii=False,
            )
        )

    def check(self):
        if not self.handler.healthy or not self.harmful_handler.healthy:
            raise GuardError("日志无法写入，已停止自动处理。")
        if shutil.disk_usage(self.root).free < 256 * 1024 * 1024:
            raise GuardError("数据盘空间不足256MiB，已停止自动处理。")

    def maintain(self, normal_days, harmful_days):
        today = datetime.now(timezone(timedelta(hours=8))).date()
        for prefix, days in (("events", normal_days), ("risk", harmful_days)):
            cutoff = today - timedelta(days=days)
            handler = self.handler if prefix == "events" else self.harmful_handler
            if handler.stream and handler.day < cutoff.isoformat():
                handler.acquire()
                try:
                    handler.stream.close()
                    handler.stream = None
                finally:
                    handler.release()
            for path in (self.root / "logs").glob(prefix + "-*.jsonl"):
                try:
                    day = datetime.strptime(path.name[len(prefix) + 1 : len(prefix) + 11], "%Y-%m-%d").date()
                except ValueError:
                    continue
                if day < cutoff:
                    path.unlink(missing_ok=True)

    def close(self):
        self.logger.removeHandler(self.handler)
        self.handler.close()
        self.harmful_logger.removeHandler(self.harmful_handler)
        self.harmful_handler.close()
