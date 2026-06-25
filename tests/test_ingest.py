import pytest

from hybridagent import ingest


def test_extract_plaintext(tmp_path):
    p = tmp_path / "note.txt"
    p.write_text("hello world\nsecond line", encoding="utf-8")
    doc = ingest.extract_text(p)
    assert "hello world" in doc.text
    assert doc.kind == "document"
    assert doc.source == "note.txt"


def test_extract_markdown(tmp_path):
    p = tmp_path / "doc.md"
    p.write_text("# Title\n\nbody text", encoding="utf-8")
    assert "Title" in ingest.extract_text(p).text


def test_extract_csv(tmp_path):
    p = tmp_path / "data.csv"
    p.write_text("name,role\nAlice,eng\nBob,design", encoding="utf-8")
    text = ingest.extract_text(p).text
    assert "Alice" in text and "design" in text


def test_extract_json(tmp_path):
    p = tmp_path / "obj.json"
    p.write_text('{"a": 1, "b": [2, 3]}', encoding="utf-8")
    assert '"a"' in ingest.extract_text(p).text


def test_extract_html(tmp_path):
    p = tmp_path / "page.html"
    p.write_text("<html><body><h1>Hi</h1><script>x=1</script><p>para</p></body></html>",
                 encoding="utf-8")
    text = ingest.extract_text(p).text
    assert "Hi" in text and "para" in text
    assert "x=1" not in text                      # script content stripped


def test_extract_eml(tmp_path):
    p = tmp_path / "msg.eml"
    p.write_text(
        "From: a@example.com\nTo: b@example.com\nSubject: Sync notes\n"
        "Content-Type: text/plain\n\nThe customer asked for a follow-up.\n",
        encoding="utf-8")
    doc = ingest.extract_text(p)
    assert doc.kind == "email"
    assert "Sync notes" in doc.text
    assert "follow-up" in doc.text


def test_unsupported_suffix_raises(tmp_path):
    p = tmp_path / "x.bin"
    p.write_bytes(b"\x00\x01")
    with pytest.raises(ValueError):
        ingest.extract_text(p)


def test_missing_file_raises():
    with pytest.raises(FileNotFoundError):
        ingest.extract_text("does-not-exist.txt")


def test_is_supported():
    assert ingest.is_supported("a.pdf")
    assert ingest.is_supported("a.docx")
    assert not ingest.is_supported("a.bin")


# ---- optional rich-format parsers (skip cleanly when deps not installed) ----
def test_extract_docx(tmp_path):
    docx = pytest.importorskip("docx")
    p = tmp_path / "d.docx"
    d = docx.Document()
    d.add_paragraph("project goals and milestones")
    d.save(str(p))
    assert "milestones" in ingest.extract_text(p).text


def test_extract_pptx(tmp_path):
    pptx = pytest.importorskip("pptx")
    p = tmp_path / "d.pptx"
    prs = pptx.Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[5])
    slide.shapes.title.text = "Roadmap overview"
    prs.save(str(p))
    assert "Roadmap" in ingest.extract_text(p).text


def test_extract_xlsx(tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    p = tmp_path / "d.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["owner", "task"])
    ws.append(["Alice", "ship the report"])
    wb.save(str(p))
    assert "ship the report" in ingest.extract_text(p).text


def test_extract_pdf(tmp_path):
    pytest.importorskip("pypdf")
    # Build a 1-page PDF with text using pypdf's writer if reportlab is absent.
    reportlab = pytest.importorskip("reportlab")
    from reportlab.pdfgen import canvas
    p = tmp_path / "d.pdf"
    c = canvas.Canvas(str(p))
    c.drawString(72, 720, "quarterly milestones and owners")
    c.save()
    assert "milestones" in ingest.extract_text(p).text
