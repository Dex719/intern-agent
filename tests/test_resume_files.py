"""Извлечение текста резюме из файлов + эндпоинт загрузки."""

import io
import zipfile

import pytest
from fastapi.testclient import TestClient

from intern_agent import config, resume_files
from intern_agent.api.app import app

RESUME_TEXT = "Иван Иванов — python-стажёр. Опыт: pet-проекты на FastAPI, SQLite, Docker. Ищу стажировку в Алматы."


# ---------- фикстуры-файлы ----------


def make_pdf(text: str) -> bytes:
    """Минимальный валидный PDF с одной строкой текста (latin-1)."""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode("latin-1")
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for i, obj in enumerate(objs, start=1):
        offsets.append(out.tell())
        out.write(f"{i} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref_pos = out.tell()
    out.write(f"xref\n0 {len(objs) + 1}\n".encode())
    out.write(b"0000000000 65535 f \n")
    for off in offsets:
        out.write(f"{off:010d} 00000 n \n".encode())
    out.write(
        f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF\n".encode()
    )
    return out.getvalue()


def make_docx(paragraphs: list[str]) -> bytes:
    """Минимальный DOCX: zip с word/document.xml."""
    ns = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    xml = f'<?xml version="1.0" encoding="UTF-8"?><w:document {ns}><w:body>{body}</w:body></w:document>'
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", xml)
    return out.getvalue()


# ---------- extract_text ----------


def test_extract_txt_utf8():
    text = resume_files.extract_text("resume.txt", RESUME_TEXT.encode("utf-8"))
    assert "python-стажёр" in text


def test_extract_txt_cp1251():
    text = resume_files.extract_text("resume.txt", RESUME_TEXT.encode("cp1251"))
    assert "Иван Иванов" in text


def test_extract_md_normalizes_whitespace():
    raw = "# Иван\r\n\r\n\r\nPython,   SQL\t\tDocker".encode("utf-8")
    text = resume_files.extract_text("resume.md", raw)
    assert text == "# Иван\n\nPython, SQL Docker"


def test_extract_docx():
    data = make_docx(["Иван Иванов", "Навыки: Python, FastAPI", "Опыт: pet-проекты"])
    text = resume_files.extract_text("resume.docx", data)
    assert "Иван Иванов" in text
    assert "Навыки: Python, FastAPI" in text
    assert text.count("\n") == 2  # параграфы -> строки


def test_extract_pdf():
    data = make_pdf("Ivan Ivanov - Python intern. FastAPI, SQL, Docker pet projects.")
    text = resume_files.extract_text("resume.pdf", data)
    assert "Python intern" in text


def test_unsupported_extension():
    with pytest.raises(resume_files.ResumeFileError, match="PDF, DOCX, MD и TXT"):
        resume_files.extract_text("resume.rtf", b"{\\rtf1 hello}")


def test_empty_and_oversized():
    with pytest.raises(resume_files.ResumeFileError):
        resume_files.extract_text("resume.txt", b"")
    with pytest.raises(resume_files.ResumeFileError, match="10 МБ"):
        resume_files.extract_text("resume.txt", b"x" * (resume_files.MAX_FILE_SIZE + 1))


def test_broken_docx_and_pdf():
    with pytest.raises(resume_files.ResumeFileError):
        resume_files.extract_text("resume.docx", b"not a zip at all")
    with pytest.raises(resume_files.ResumeFileError):
        resume_files.extract_text("resume.pdf", b"%PDF-1.4 broken")


# ---------- POST /api/resume/upload ----------


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")
    with TestClient(app) as test_client:
        yield test_client


def test_upload_md_saves_resume(client):
    resp = client.post(
        "/api/resume/upload",
        files={"file": ("resume.md", RESUME_TEXT.encode("utf-8"), "text/markdown")},
    )
    assert resp.status_code == 200
    assert "FastAPI" in resp.json()["content"]
    data = client.get("/api/resume").json()
    assert data["has_resume"] is True
    assert "python-стажёр" in data["content"]


def test_upload_docx_saves_resume(client):
    docx = make_docx([RESUME_TEXT, "Образование: 2 курс, КазНУ"])
    resp = client.post(
        "/api/resume/upload",
        files={"file": ("cv.docx", docx, "application/octet-stream")},
    )
    assert resp.status_code == 200
    assert "КазНУ" in resp.json()["content"]


def test_upload_rejects_bad_files(client):
    resp = client.post(
        "/api/resume/upload", files={"file": ("virus.exe", b"MZ\x90\x00", "application/x-msdownload")}
    )
    assert resp.status_code == 400

    resp = client.post("/api/resume/upload", files={"file": ("short.txt", "коротко".encode(), "text/plain")})
    assert resp.status_code == 400
    assert client.get("/api/resume").json()["has_resume"] is False
