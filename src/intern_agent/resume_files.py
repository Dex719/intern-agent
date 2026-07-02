"""Извлечение текста резюме из файлов: PDF, DOCX, Markdown, TXT."""

import io
import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree

MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 МБ
ALLOWED_EXTENSIONS = (".pdf", ".docx", ".md", ".txt")

# Пространство имён WordprocessingML (docx).
_W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


class ResumeFileError(Exception):
    """Не удалось прочитать файл резюме."""


def extract_text(filename: str, data: bytes) -> str:
    """Достаёт плоский текст резюме из файла по расширению."""
    if not data:
        raise ResumeFileError("Файл пустой")
    if len(data) > MAX_FILE_SIZE:
        raise ResumeFileError("Файл больше 10 МБ — сохрани резюме компактнее")
    ext = Path(filename or "").suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise ResumeFileError("Поддерживаются только PDF, DOCX, MD и TXT")
    if ext == ".pdf":
        text = _from_pdf(data)
    elif ext == ".docx":
        text = _from_docx(data)
    else:
        text = _from_plain(data)
    text = _normalize(text)
    if not text:
        raise ResumeFileError("Не нашёл текст в файле — если это скан, вставь текст вручную")
    return text


def _from_pdf(data: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover — защита от кривой установки
        raise ResumeFileError("PDF-поддержка не установлена (пакет pypdf)") from exc
    try:
        reader = PdfReader(io.BytesIO(data), strict=False)
        if reader.is_encrypted:
            try:
                reader.decrypt("")
            except Exception as exc:
                raise ResumeFileError("PDF под паролем — сними защиту и загрузи снова") from exc
        pages = [page.extract_text() or "" for page in reader.pages]
    except ResumeFileError:
        raise
    except Exception as exc:
        raise ResumeFileError("Не удалось прочитать PDF — файл повреждён?") from exc
    return "\n".join(pages)


def _from_docx(data: bytes) -> str:
    """DOCX = zip с word/document.xml; собираем текст по параграфам (включая таблицы)."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            xml = archive.read("word/document.xml")
        root = ElementTree.fromstring(xml)
    except (zipfile.BadZipFile, KeyError, ElementTree.ParseError) as exc:
        raise ResumeFileError("Не удалось прочитать DOCX — файл повреждён?") from exc
    paragraphs: list[str] = []
    for para in root.iter(f"{_W_NS}p"):
        parts: list[str] = []
        for node in para.iter():
            if node.tag == f"{_W_NS}t" and node.text:
                parts.append(node.text)
            elif node.tag in (f"{_W_NS}br", f"{_W_NS}tab"):
                parts.append(" ")
        if parts:
            paragraphs.append("".join(parts))
    return "\n".join(paragraphs)


def _from_plain(data: bytes) -> str:
    for encoding in ("utf-8", "cp1251"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _normalize(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()
