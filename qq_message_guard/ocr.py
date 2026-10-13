"""Bounded, local OCR using Windows' built-in engine or Tesseract."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from .config import GuardError


@dataclass(frozen=True)
class OcrResult:
    text: str
    backend: str
    elapsed_ms: float
    truncated: bool = False


class Ocr:
    @staticmethod
    def command(path):
        if os.name == "nt":
            executable = shutil.which("powershell.exe")
            backend = "Windows.Media.Ocr zh-Hans-CN"
            command = [
                executable or "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(Path(__file__).with_name("windows_ocr.ps1")),
                "-ImagePath",
                str(path.resolve()),
            ]
        else:
            executable = shutil.which("tesseract")
            backend = "Tesseract chi_sim+eng"
            command = [executable or "tesseract", str(path), "stdout", "-l", "chi_sim+eng"]
        if not executable:
            raise GuardError("本机OCR不可用：Windows需中文OCR语言包，其他系统需Tesseract及中文语言包。")
        return command, backend

    async def recognize(self, path, settings):
        command, backend = self.command(path)
        started = time.monotonic()
        process = None
        try:
            process = await asyncio.create_subprocess_exec(
                *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            stdout, _ = await asyncio.wait_for(process.communicate(), settings.ocr_timeout)
            if process.returncode != 0 or len(stdout) > 1024 * 1024:
                raise GuardError("本机OCR未完成，请检查中文语言包或本地OCR安装。")
            output = stdout.decode("utf-8-sig", errors="strict").strip()
            if os.name == "nt":
                decoded = json.loads(output)
                if not isinstance(decoded, dict):
                    raise ValueError()
                text = decoded.get("text")
            else:
                text = output
            if not isinstance(text, str):
                raise ValueError()
        except asyncio.TimeoutError as exc:
            raise GuardError("本机OCR超时，本次未完成图片文字提取。") from exc
        except (OSError, ValueError, KeyError) as exc:
            raise GuardError("本机OCR失败，本次未完成图片文字提取。") from exc
        finally:
            if process is not None and process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                await asyncio.shield(process.communicate())
        limit = settings.advanced.max_text_chars
        return OcrResult(
            text[:limit], backend, round((time.monotonic() - started) * 1000, 2), len(text) > limit
        )
