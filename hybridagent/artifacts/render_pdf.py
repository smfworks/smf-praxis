"""Optional deterministic PDF renderer for professional artifacts."""
from __future__ import annotations

import io
from html import escape
from typing import Any

from hybridagent.artifacts.models import (
    ArtifactDocument,
    FigureBlock,
    ListBlock,
    PageBreakBlock,
    ParagraphBlock,
    TableBlock,
)
from hybridagent.artifacts.render_common import (
    ArtifactRenderError,
    checked_assets,
    image_kind,
    require_backend,
)


def render_pdf(
    document: ArtifactDocument, assets: dict[str, bytes] | None = None
) -> bytes:
    require_backend("reportlab", "reportlab")
    from reportlab.lib import colors  # type: ignore[import-untyped]
    from reportlab.lib.enums import TA_CENTER  # type: ignore[import-untyped]
    from reportlab.lib.pagesizes import LETTER  # type: ignore[import-untyped]
    from reportlab.lib.styles import (  # type: ignore[import-untyped]
        ParagraphStyle,
        getSampleStyleSheet,
    )
    from reportlab.lib.units import inch  # type: ignore[import-untyped]
    from reportlab.pdfgen import canvas as canvas_module  # type: ignore[import-untyped]
    from reportlab.platypus import (  # type: ignore[import-untyped]
        Image,
        ListFlowable,
        ListItem,
        PageBreak,
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )

    resolved = checked_assets(document, assets)
    output = io.BytesIO()
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name="ArtifactTitle", parent=styles["Title"], alignment=TA_CENTER))
    styles.add(ParagraphStyle(name="ArtifactCaption", parent=styles["BodyText"], fontSize=9))
    story: list[Any] = [
        Paragraph(escape(document.metadata.title), styles["ArtifactTitle"]),
        Paragraph(escape(f"Classification: {document.metadata.confidentiality.upper()}"), styles["Normal"]),
        Spacer(1, 0.2 * inch),
    ]
    if document.metadata.subject:
        story.extend((Paragraph(escape(f"Subject: {document.metadata.subject}"), styles["Normal"]),
                      Spacer(1, 0.1 * inch)))

    def add_sections(sections: tuple[Any, ...]) -> None:
        for section in sections:
            heading = styles[f"Heading{min(6, section.level)}"]
            story.append(Paragraph(escape(section.title), heading))
            for block in section.blocks:
                suffix = "" if not getattr(block, "citation_ids", ()) else " [" + ", ".join(block.citation_ids) + "]"
                if type(block) is ParagraphBlock:
                    story.extend((Paragraph(escape(block.text + suffix), styles["BodyText"]), Spacer(1, 0.08 * inch)))
                elif type(block) is ListBlock:
                    story.append(ListFlowable(
                        [ListItem(Paragraph(escape(item), styles["BodyText"])) for item in block.items],
                        bulletType="1" if block.ordered else "bullet",
                    ))
                    if suffix:
                        story.append(Paragraph(escape(suffix.strip()), styles["BodyText"]))
                elif type(block) is TableBlock:
                    data = [list(block.columns), *[list(row) for row in block.rows]]
                    table = Table(data, repeatRows=1, hAlign="LEFT")
                    table.setStyle(TableStyle([
                        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E8EDF3")),
                        ("TEXTCOLOR", (0, 0), (-1, 0), colors.black),
                        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                        ("VALIGN", (0, 0), (-1, -1), "TOP"),
                        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                    ]))
                    if block.caption:
                        story.append(Paragraph(escape(block.caption + suffix), styles["ArtifactCaption"]))
                    story.extend((table, Spacer(1, 0.12 * inch)))
                elif type(block) is FigureBlock:
                    payload = resolved[block.asset_id]
                    if image_kind(payload) == "svg":
                        raise ArtifactRenderError("PDF rendering requires PNG or JPEG figure assets")
                    image = Image(io.BytesIO(payload), width=5.5 * inch, height=3.5 * inch, kind="proportional")
                    image.hAlign = "LEFT"
                    story.extend((image, Paragraph(escape((block.caption or block.alt_text) + suffix), styles["ArtifactCaption"])))
                elif type(block) is PageBreakBlock:
                    story.append(PageBreak())

    add_sections(document.sections)
    if document.appendices:
        story.extend((PageBreak(), Paragraph("Appendices", styles["Heading1"])))
        add_sections(document.appendices)
    if document.sources:
        story.extend((PageBreak(), Paragraph("Source Manifest", styles["Heading1"])))
        source_data = [["Source", "Version", "SHA-256", "Title"]]
        source_data.extend([[item.source_id, item.source_version_id, item.content_hash, item.title]
                            for item in document.sources])
        source_table = Table(source_data, repeatRows=1)
        source_table.setStyle(TableStyle([
            ("GRID", (0, 0), (-1, -1), 0.4, colors.grey),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 7),
        ]))
        story.append(source_table)

    class InvariantCanvas(canvas_module.Canvas):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            kwargs["invariant"] = 1
            super().__init__(*args, **kwargs)
            self.setTitle(document.metadata.title)
            self.setAuthor(document.metadata.created_by)
            self.setSubject(document.metadata.subject)
            self.setCreator("Praxis Artifact Studio")

    def footer(canvas: Any, doc: Any) -> None:
        canvas.saveState()
        canvas.setFont("Helvetica", 8)
        canvas.drawString(doc.leftMargin, 0.45 * inch,
                          f"{document.metadata.confidentiality.upper()} — {document.artifact_id}")
        canvas.drawRightString(LETTER[0] - doc.rightMargin, 0.45 * inch, str(doc.page))
        canvas.restoreState()

    template = SimpleDocTemplate(
        output, pagesize=LETTER, title=document.metadata.title,
        author=document.metadata.created_by, subject=document.metadata.subject,
        leftMargin=0.7 * inch, rightMargin=0.7 * inch,
        topMargin=0.7 * inch, bottomMargin=0.7 * inch,
    )
    template.build(story, onFirstPage=footer, onLaterPages=footer, canvasmaker=InvariantCanvas)
    return output.getvalue()
