"""Local evidence archives and bounded, offline chat-style review pictures.

QQ receives only rendered text. Original attachment pixels stay in the local
archive. Decorative lines are a presentation option, not a delivery guarantee.
"""

from __future__ import annotations

import asyncio
import html
import io
import json
import os
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .config import GuardError
from .lexicon import matches

WIDTH, HEIGHT, MAX_PAGES = 1100, 2600, 4
FONT_SIZE, LINE_HEIGHT = 36, 56
COLORS = ("#94bddb", "#c7a1c7")


def readable(value):
    return "".join(c for c in str(value) if c in "\n\t" or not unicodedata.category(c).startswith("C"))


def timestamp(at):
    value = datetime.fromtimestamp(at, timezone(timedelta(hours=8)))
    return f"{value.month}月{value.day}日 {value.hour:02d}:{value.minute:02d}:{value.second:02d}"


def original_text(raw):
    """Keep more of the received publication than the model's text budget."""
    parts = []
    for segment in raw["message"]:
        kind, data = segment["type"], segment["data"]
        if kind == "text" and isinstance(data.get("text"), str):
            parts.append(data["text"])
        elif kind == "at":
            parts.append("@" + str(data.get("qq", ""))[:20])
        elif kind == "face":
            parts.append("[QQ表情]")
    text = "".join(parts)
    return {"text": text[:20000], "truncated": len(text) > 20000}


def evidence_path(root, relative):
    """Accept only flat files owned by this plugin, never arbitrary local paths."""
    path = Path(relative)
    if (
        path.is_absolute()
        or len(path.parts) != 2
        or path.parts[0] not in ("evidence", "media")
        or ".." in path.name
        or "\\" in relative
        or ":" in relative
    ):
        raise GuardError("证据文件路径无效。")
    resolved = (root / path).resolve()
    if resolved.parent != (root / path.parts[0]).resolve():
        raise GuardError("证据文件路径无效。")
    return resolved


def atomic_write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    try:
        with temporary.open("wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def find_font(configured):
    if configured:
        candidates = [Path(configured)]
    else:
        windows = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
        candidates = [
            windows / "msyh.ttc",
            windows / "simhei.ttf",
            Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
            Path("/usr/share/fonts/truetype/wqy/wqy-microhei.ttc"),
            Path("/System/Library/Fonts/PingFang.ttc"),
            Path("/mnt/c/Windows/Fonts/msyh.ttc"),
        ]
    for path in candidates:
        if path.is_file():
            return path
    raise GuardError("没有可用的中文字体，请在证据图片配置中填写本机字体路径。")


def wrap(text, font, width):
    lines, current = [], ""
    for character in readable(text).replace("\t", "  "):
        if character == "\n":
            lines.append(current)
            current = ""
        elif current and (len(current) >= 120 or font.getlength(current + character) > width):
            lines.append(current)
            current = character
        else:
            current += character
    lines.append(current)
    return lines


def source_data(case):
    data, message = case["data"], case["data"]["message"]
    original = data.get("original_text", {"text": message["text"], "truncated": False})
    return {
        "id": case["id"],
        "message": {
            **{
                k: message.get(k, "")
                for k in ("platform", "account", "group", "user", "mid", "at", "sender_name")
            },
            **original,
        },
        "context": data.get("context", []),
        "media": data.get("media_records", []),
        "ocr": data.get("ocr_records", []),
        "image_count": data.get("original_image_count", len(data.get("media_records", []))),
        "unsupported": message.get("unsupported", []),
    }


def local_html(source):
    def bubble(title, text, kind=""):
        return f'<section class="{kind}"><header>{html.escape(title)}</header><pre>{html.escape(text)}</pre></section>'

    message = source["message"]
    content = []
    for row in source["context"]:
        label = "实际引用" if row.get("quoted") else "前文"
        author = row.get("sender_name") or f"QQ {row['user']}"
        content.append(bubble(f"{label} · {author} · {timestamp(row['at'])}", row["text"]))
    content.append(
        bubble(f"{message['sender_name'] or '群成员'}（{message['user']}）", message["text"], "current")
    )
    if message["truncated"]:
        content.append(
            bubble("留存边界", "原消息超过20000字，仅留存开头；数据库中的审核结果不代表完整覆盖。")
        )
    for media in source["media"]:
        relative = media.get("file", "")
        if (
            relative.startswith("media/")
            and len(Path(relative).parts) == 2
            and ".." not in relative
            and "\\" not in relative
        ):
            content.append(
                f'<section><header>本地审核图片</header><img src="../{html.escape(relative, quote=True)}"></section>'
            )
    for row in source["ocr"]:
        content.append(bubble("本机 OCR（可能识别错误）", row["text"]))
    return (
        '<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        "<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; img-src 'self' file:; style-src 'unsafe-inline'\">"
        f"<title>审核记录 #{source['id']}</title><style>"
        'body{margin:0;background:#eef2f7;color:#253246;font:18px "Microsoft YaHei","Noto Sans CJK SC",sans-serif}'
        "main{max-width:900px;margin:auto;padding:24px}h1{font-size:26px}section{background:white;border-radius:16px;padding:20px;margin:16px 0}"
        "header{color:#59677d;margin-bottom:12px}.current{border-left:5px solid #5682b2}pre{font:inherit;white-space:pre-wrap;overflow-wrap:anywhere}"
        "img{max-width:100%;height:auto}</style><main>"
        f"<h1>审核记录 #{source['id']}</h1><p>群 {html.escape(str(message['group']))} · {timestamp(message['at'])}"
        f" · 消息 {html.escape(str(message['mid']))}</p>"
        "<p>本地原始证据；前文与引用可能受接收时的缓存字数限制。处理结果以插件审核记录为准。</p>"
        + "".join(content)
        + "</main></html>"
    )


@dataclass(frozen=True)
class Pictures:
    preview: tuple[Path, ...]
    clear: tuple[Path, ...]


class EvidenceArchive:
    def __init__(self, store):
        self.store, self.root = store, store.path.parent
        self.lock = asyncio.Lock()

    @staticmethod
    def files(cid):
        paths = [f"evidence/{cid}-source.json", f"evidence/{cid}-local.html"] + [
            f"evidence/{cid}-{kind}-{page}.png"
            for kind in ("clear", "notice")
            for page in range(1, MAX_PAGES + 1)
        ]
        return paths + [path + ".tmp" for path in paths]

    async def thread(self, function, *args):
        # A cancelled to_thread keeps running. Wait before allowing cleanup or reload.
        task = asyncio.create_task(asyncio.to_thread(function, *args))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await asyncio.gather(task, return_exceptions=True)
            raise

    async def _archive(self, case):
        await self.store.call("evidence_files", case["id"], self.files(case["id"]))
        source = source_data(case)
        try:
            await self.thread(self._write_source, source)
        except (OSError, ValueError) as exc:
            raise GuardError("本地证据保存失败，未提交新的撤回操作。") from exc
        return source

    def _write_source(self, source):
        cid = source["id"]
        atomic_write(
            self.root / f"evidence/{cid}-source.json",
            json.dumps(source, ensure_ascii=False, indent=2).encode(),
        )
        atomic_write(self.root / f"evidence/{cid}-local.html", local_html(source).encode())

    async def archive(self, case):
        async with self.lock:
            return await self._archive(case)

    async def render(self, case, settings, group_name="", terms=()):
        async with self.lock:
            source = await self._archive(case)
            try:
                return await self.thread(self._render, source, settings, group_name, terms)
            except (OSError, ValueError) as exc:
                raise GuardError("证据图片生成失败；原文仍保存在本地，不会回退发送原文。") from exc

    def _render(self, source, settings, group_name, terms):
        font_path = find_font(settings.font_path)
        font = ImageFont.truetype(str(font_path), FONT_SIZE)
        small = ImageFont.truetype(str(font_path), 28)
        message, cid = source["message"], source["id"]

        def display_name(name, fallback):
            value = readable(name)[:40]
            return value if value.strip() and not matches(value, terms) else fallback

        blocks = []
        background = [row for row in source["context"] if not row.get("quoted")]
        background = background[-settings.context_messages :] if settings.context_messages else []
        for row in background:
            who = display_name(row.get("sender_name", ""), f"QQ {row['user']}")
            blocks.append((f"前文节选 · {who} · {timestamp(row['at'])}", row["text"][:160]))
        for row in source["context"]:
            if row.get("quoted"):
                blocks.append((f"实际引用 · QQ {row['user']}", row["text"][:500]))
        text = message["text"][:4000]
        if len(message["text"]) > 4000 or message["truncated"]:
            text += "\n〔文字超过图片预览上限，完整留存范围请查看本地证据〕"
        author = display_name(message["sender_name"], "群成员")
        blocks.append((f"本条发言 · {author}（{message['user']}）", text or "〔本条没有文字〕"))
        if source["image_count"]:
            blocks.append(
                ("附件", f"共 {source['image_count']} 张图片；原始附件不随通知转发，请查看本地证据。")
            )
        for row in source["ocr"]:
            ocr_text = row["text"][:800]
            if len(row["text"]) > 800 or row.get("truncated"):
                ocr_text += "\n〔OCR文字已节选〕"
            blocks.append(("图片文字 · 本机 OCR，可能有误", ocr_text))
        if source["unsupported"]:
            blocks.append(("留存说明", "部分消息段不支持，请结合本地记录判断。"))

        rows = []
        for title, body in blocks:
            rows.extend((line, True) for line in wrap(title, small, WIDTH - 112))
            rows.extend((line, False) for line in wrap(body, font, WIDTH - 112))
            rows.append(("", False))
        rows_per_page = (HEIGHT - 300) // LINE_HEIGHT
        capacity = rows_per_page * MAX_PAGES
        if len(rows) > capacity:
            rows = rows[: capacity - 1] + [("〔图片未展示全部内容，请查看本地证据〕", True)]
        pages = [rows[i : i + rows_per_page] for i in range(0, len(rows), rows_per_page)]
        clear_paths, preview_paths = [], []
        for page, lines in enumerate(pages, 1):
            height = 260 + LINE_HEIGHT * len(lines)
            clear = Image.new("RGB", (WIDTH, height), "#eef2f7")
            draw = ImageDraw.Draw(clear)
            draw.text((42, 25), "发言审核 · 证据", font=font, fill="#253246")
            title = readable(group_name or f"群 {message['group']}")[:22]
            draw.text(
                (42, 85), f"{title} · 记录 #{cid} · 第 {page}/{len(pages)} 页", font=small, fill="#59677d"
            )
            draw.text(
                (42, 130), f"{timestamp(message['at'])} · 群 {message['group']}", font=small, fill="#59677d"
            )
            draw.rounded_rectangle((28, 182, WIDTH - 28, height - 70), 22, fill="white")
            spans = []
            for i, (line, heading) in enumerate(lines):
                x, y = 56, 198 + i * LINE_HEIGHT
                chosen = small if heading else font
                draw.text((x, y), line, font=chosen, fill="#59677d" if heading else "#202020")
                if line and not heading:
                    spans.append(draw.textbbox((x, y), line, font=chosen))
            draw.text(
                (42, height - 55),
                f"私聊 /消息审核 查看 {cid}；本地文件保留清晰原文",
                font=small,
                fill="#59677d",
            )
            preview = clear.copy()
            if settings.line_width:
                overlay = ImageDraw.Draw(preview)
                for left, top, right, bottom in spans:
                    for fraction, color in zip((0.3, 0.7), COLORS):
                        y = round(top + (bottom - top) * fraction)
                        overlay.line((left - 5, y, right + 5, y), fill=color, width=settings.line_width)
            for kind, picture, output in (("clear", clear, clear_paths), ("notice", preview, preview_paths)):
                path = self.root / f"evidence/{cid}-{kind}-{page}.png"
                buffer = io.BytesIO()
                picture.save(buffer, format="PNG")
                atomic_write(path, buffer.getvalue())
                output.append(path)
        # Regenerating a shorter record must not retain stale pages with old text.
        for page in range(len(pages) + 1, MAX_PAGES + 1):
            for kind in ("clear", "notice"):
                (self.root / f"evidence/{cid}-{kind}-{page}.png").unlink(missing_ok=True)
        return Pictures(tuple(preview_paths), tuple(clear_paths))
