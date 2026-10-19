"""Разбиение документов runbook'ов на чанки для RAG.

Контракт (его проверяют тесты):

* `chunk_markdown(text, max_chars=..., overlap=...)` возвращает список
  `Chunk(index, heading, text)`, индексы 0..N-1 подряд;
* `len(chunk.text) <= max_chars` — за счёт того, что «хлебная крошка»
  заголовка вычитается из бюджета тела;
* текст не теряется: объединение чанков покрывает все непустые абзацы
  (перекрытие — не потеря, а повтор хвоста);
* пустой/пробельный документ -> пустой список;
* заголовки Markdown (`# ... ######`) становятся `heading` чанка в формате
  «Раздел > Подраздел»; текст самого заголовка в тело не дублируется.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_PARAGRAPH_SPLIT_RE = re.compile(r"\n\s*\n+")


@dataclass
class Chunk:
    index: int
    heading: str
    text: str


def _sections(text: str) -> list[tuple[str, str]]:
    """Текст -> [(путь заголовков, тело)] в порядке появления."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    stack: list[tuple[int, str]] = []   # (уровень, заголовок)
    sections: list[tuple[str, str]] = []
    body: list[str] = []

    def flush() -> None:
        body_text = "\n".join(body).strip()
        if body_text:
            heading = " > ".join(title for _, title in stack)
            sections.append((heading, body_text))
        body.clear()

    for line in lines:
        match = _HEADING_RE.match(line)
        if match:
            flush()
            level = len(match.group(1))
            title = match.group(2).strip()
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, title))
        else:
            body.append(line)
    flush()
    return sections


def _split_long(paragraph: str, limit: int) -> list[str]:
    """Абзац длиннее лимита — режем жёстко по символам (по возможности по словам)."""
    pieces: list[str] = []
    rest = paragraph
    while len(rest) > limit:
        cut = rest.rfind(" ", 0, limit)
        if cut < limit // 2:            # слово не нашлось близко к границе
            cut = limit
        pieces.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip()
    if rest:
        pieces.append(rest)
    return pieces


def _pack_paragraphs(paragraphs: list[str], limit: int) -> list[str]:
    """Жадно укладывает абзацы в чанки не длиннее limit."""
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for paragraph in paragraphs:
        pieces = _split_long(paragraph, limit) if len(paragraph) > limit else [paragraph]
        for piece in pieces:
            piece_len = len(piece) + (2 if current else 0)  # '\n\n'
            if current and current_len + piece_len > limit:
                chunks.append("\n\n".join(current))
                current, current_len = [], 0
            current.append(piece)
            current_len += piece_len
    if current:
        chunks.append("\n\n".join(current))
    return chunks


def _overlap_tail(text: str, overlap: int) -> str:
    """Хвост предыдущего чанка для перекрытия (не больше overlap символов)."""
    if overlap <= 0:
        return ""
    tail = text[-overlap:]
    cut = tail.find(" ")
    if 0 < cut < len(tail) // 3:
        tail = tail[cut + 1:]
    return tail.strip()


def chunk_markdown(text: str, *, max_chars: int = 1200,
                   overlap: int = 150) -> list[Chunk]:
    """Документ (.md/.txt) -> чанки с сохранением заголовков."""
    max_chars = max(200, int(max_chars))
    overlap = max(0, min(int(overlap), max_chars // 2))
    result: list[Chunk] = []
    index = 0
    previous_tail = ""

    for heading, body in _sections(text):
        prefix = f"{heading}\n\n" if heading else ""
        budget = max_chars - len(prefix)
        if budget < 100:                # слишком длинный заголовок — режем его
            prefix = prefix[: max_chars // 3]
            budget = max_chars - len(prefix)
        paragraphs = [p.strip() for p in _PARAGRAPH_SPLIT_RE.split(body) if p.strip()]

        # перекрытие с предыдущим чанком того же раздела (и между разделами тоже —
        # контекст на стыке заголовков полезен модели)
        if previous_tail and overlap:
            carry = _overlap_tail(previous_tail, overlap)
            if carry:
                cap = min(len(carry), budget // 3)
                if cap > 0:
                    paragraphs.insert(0, carry[-cap:])

        for body_chunk in _pack_paragraphs(paragraphs, budget):
            chunk_text = prefix + body_chunk
            if len(chunk_text) > max_chars:
                chunk_text = chunk_text[:max_chars]
            result.append(Chunk(index=index, heading=heading, text=chunk_text))
            index += 1
            previous_tail = body_chunk

    return result
