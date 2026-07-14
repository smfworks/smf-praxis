"""Canonical dependency-free document model for Praxis Artifact Studio."""
from __future__ import annotations

import hashlib
import json
import unicodedata
from dataclasses import dataclass, fields
from typing import Any, Union

SCHEMA_VERSION = 1


class ArtifactModelError(ValueError):
    """An artifact payload cannot be represented by the canonical model."""


def _nfc(value: str, label: str, *, empty: bool = True) -> str:
    if type(value) is not str:
        raise ArtifactModelError(f"{label} must be exact text")
    result = unicodedata.normalize("NFC", value)
    if any(0xD800 <= ord(char) <= 0xDFFF for char in result):
        raise ArtifactModelError(f"{label} contains an invalid Unicode surrogate")
    if not empty and not result.strip():
        raise ArtifactModelError(f"{label} is required")
    return result


def _strict_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, item in pairs:
        if key in result:
            raise ArtifactModelError(f"artifact JSON contains duplicate member: {key}")
        result[key] = item
    return result


def _reject_json_constant(value: str) -> None:
    raise ArtifactModelError(f"artifact JSON contains non-finite number: {value}")


def _text_tuple(value: tuple[str, ...], label: str) -> tuple[str, ...]:
    if type(value) is not tuple:
        raise ArtifactModelError(f"{label} must be a tuple")
    return tuple(_nfc(item, f"{label} item") for item in value)


def _expect_object(value: Any, label: str, required: set[str], optional: set[str]) -> dict[str, Any]:
    if type(value) is not dict or any(type(key) is not str for key in value):
        raise ArtifactModelError(f"{label} must be an exact object")
    keys = set(value)
    missing = sorted(required - keys)
    extra = sorted(keys - required - optional)
    if missing:
        raise ArtifactModelError(f"{label} is missing fields: {missing}")
    if extra:
        raise ArtifactModelError(f"{label} has unknown fields: {extra}")
    return value


def _expect_list(value: Any, label: str) -> list[Any]:
    if type(value) is not list:
        raise ArtifactModelError(f"{label} must be an exact array")
    return value


def _exact_int(value: Any, label: str) -> int:
    if type(value) is not int:
        raise ArtifactModelError(f"{label} must be an exact integer")
    return value


def _exact_bool(value: Any, label: str) -> bool:
    if type(value) is not bool:
        raise ArtifactModelError(f"{label} must be an exact boolean")
    return value


@dataclass(frozen=True)
class DocumentMetadata:
    title: str
    language: str
    document_type: str
    confidentiality: str
    organization_id: str
    workspace_id: str
    created_by: str
    created_at: str
    subject: str = ""

    def __post_init__(self) -> None:
        for field in fields(self):
            object.__setattr__(self, field.name, _nfc(getattr(self, field.name), field.name))


@dataclass(frozen=True)
class Citation:
    citation_id: str
    source_id: str
    span_ids: tuple[str, ...]
    claim_ids: tuple[str, ...] = ()
    pinpoint: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "citation_id", _nfc(self.citation_id, "citation_id"))
        object.__setattr__(self, "source_id", _nfc(self.source_id, "source_id"))
        object.__setattr__(self, "span_ids", _text_tuple(self.span_ids, "span_ids"))
        object.__setattr__(self, "claim_ids", _text_tuple(self.claim_ids, "claim_ids"))
        object.__setattr__(self, "pinpoint", _nfc(self.pinpoint, "pinpoint"))


@dataclass(frozen=True)
class SourceManifestEntry:
    source_id: str
    source_version_id: str
    content_hash: str
    title: str = ""
    canonical_uri: str = ""

    def __post_init__(self) -> None:
        for field in fields(self):
            object.__setattr__(self, field.name, _nfc(getattr(self, field.name), field.name))


@dataclass(frozen=True)
class RevisionRecord:
    revision_id: str
    sequence: int
    author_id: str
    created_at: str
    summary: str
    parent_hash: str = ""

    def __post_init__(self) -> None:
        if type(self.sequence) is not int:
            raise ArtifactModelError("revision sequence must be an exact integer")
        for name in ("revision_id", "author_id", "created_at", "summary", "parent_hash"):
            object.__setattr__(self, name, _nfc(getattr(self, name), name))


@dataclass(frozen=True)
class ReviewRecord:
    review_id: str
    review_type: str
    decision: str
    reviewer_id: str
    reviewed_at: str

    def __post_init__(self) -> None:
        for field in fields(self):
            object.__setattr__(self, field.name, _nfc(getattr(self, field.name), field.name))


@dataclass(frozen=True)
class SignatureRecord:
    signature_id: str
    signer_id: str
    role: str
    signed_at: str
    meaning: str
    review_id: str = ""

    def __post_init__(self) -> None:
        for field in fields(self):
            object.__setattr__(self, field.name, _nfc(getattr(self, field.name), field.name))


@dataclass(frozen=True)
class ParagraphBlock:
    block_id: str
    text: str
    citation_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "block_id", _nfc(self.block_id, "block_id"))
        object.__setattr__(self, "text", _nfc(self.text, "paragraph text"))
        object.__setattr__(self, "citation_ids", _text_tuple(self.citation_ids, "citation_ids"))


@dataclass(frozen=True)
class ListBlock:
    block_id: str
    items: tuple[str, ...]
    ordered: bool = False
    citation_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "block_id", _nfc(self.block_id, "block_id"))
        object.__setattr__(self, "items", _text_tuple(self.items, "list items"))
        if type(self.ordered) is not bool:
            raise ArtifactModelError("ordered must be an exact boolean")
        object.__setattr__(self, "citation_ids", _text_tuple(self.citation_ids, "citation_ids"))


@dataclass(frozen=True)
class TableBlock:
    block_id: str
    columns: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]
    caption: str = ""
    citation_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "block_id", _nfc(self.block_id, "block_id"))
        object.__setattr__(self, "columns", _text_tuple(self.columns, "table columns"))
        if type(self.rows) is not tuple:
            raise ArtifactModelError("table rows must be a tuple")
        object.__setattr__(self, "rows", tuple(_text_tuple(row, "table row") for row in self.rows))
        object.__setattr__(self, "caption", _nfc(self.caption, "table caption"))
        object.__setattr__(self, "citation_ids", _text_tuple(self.citation_ids, "citation_ids"))


@dataclass(frozen=True)
class FigureBlock:
    block_id: str
    asset_id: str
    media_type: str
    alt_text: str
    caption: str = ""
    citation_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in ("block_id", "asset_id", "media_type", "alt_text", "caption"):
            object.__setattr__(self, name, _nfc(getattr(self, name), name))
        object.__setattr__(self, "citation_ids", _text_tuple(self.citation_ids, "citation_ids"))


@dataclass(frozen=True)
class PageBreakBlock:
    block_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "block_id", _nfc(self.block_id, "block_id"))


Block = Union[ParagraphBlock, ListBlock, TableBlock, FigureBlock, PageBreakBlock]
_BLOCK_TYPES: dict[str, type[Block]] = {
    "paragraph": ParagraphBlock,
    "list": ListBlock,
    "table": TableBlock,
    "figure": FigureBlock,
    "page_break": PageBreakBlock,
}
_BLOCK_NAMES = {value: key for key, value in _BLOCK_TYPES.items()}


@dataclass(frozen=True)
class Section:
    section_id: str
    title: str
    level: int
    blocks: tuple[Block, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "section_id", _nfc(self.section_id, "section_id"))
        object.__setattr__(self, "title", _nfc(self.title, "section title"))
        if type(self.level) is not int:
            raise ArtifactModelError("section level must be an exact integer")
        if type(self.blocks) is not tuple or any(type(block) not in _BLOCK_NAMES for block in self.blocks):
            raise ArtifactModelError("section blocks must be exact artifact block values")


@dataclass(frozen=True)
class ArtifactDocument:
    artifact_id: str
    metadata: DocumentMetadata
    sections: tuple[Section, ...]
    appendices: tuple[Section, ...] = ()
    citations: tuple[Citation, ...] = ()
    sources: tuple[SourceManifestEntry, ...] = ()
    revisions: tuple[RevisionRecord, ...] = ()
    reviews: tuple[ReviewRecord, ...] = ()
    signatures: tuple[SignatureRecord, ...] = ()
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "artifact_id", _nfc(self.artifact_id, "artifact_id"))
        if type(self.metadata) is not DocumentMetadata:
            raise ArtifactModelError("metadata must be an exact DocumentMetadata")
        tuple_types: tuple[tuple[str, type[Any]], ...] = (
            ("sections", Section), ("appendices", Section), ("citations", Citation),
            ("sources", SourceManifestEntry), ("revisions", RevisionRecord),
            ("reviews", ReviewRecord), ("signatures", SignatureRecord),
        )
        for name, expected in tuple_types:
            value = getattr(self, name)
            if type(value) is not tuple or any(type(item) is not expected for item in value):
                raise ArtifactModelError(f"{name} must contain exact {expected.__name__} values")
        if type(self.schema_version) is not int:
            raise ArtifactModelError("schema_version must be an exact integer")

    def to_dict(self) -> dict[str, Any]:
        return _document_dict(self)

    def canonical_json(self) -> str:
        if type(self) is not ArtifactDocument:
            raise ArtifactModelError("canonical identity requires an exact ArtifactDocument")
        return json.dumps(
            _document_dict(self), ensure_ascii=False, allow_nan=False, sort_keys=True,
            separators=(",", ":"),
        )

    def canonical_bytes(self) -> bytes:
        return self.canonical_json().encode("utf-8")

    def content_hash(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

    @classmethod
    def from_dict(cls, value: Any) -> "ArtifactDocument":
        obj = _expect_object(
            value, "artifact document",
            {"schema_version", "artifact_id", "metadata", "sections", "appendices",
             "citations", "sources", "revisions", "reviews", "signatures"}, set(),
        )
        version = _exact_int(obj["schema_version"], "schema_version")
        if version != SCHEMA_VERSION:
            raise ArtifactModelError(f"unsupported artifact schema version: {version}")
        return ArtifactDocument(
            artifact_id=_nfc(obj["artifact_id"], "artifact_id"),
            metadata=_decode_metadata(obj["metadata"]),
            sections=tuple(_decode_section(item, "section") for item in _expect_list(obj["sections"], "sections")),
            appendices=tuple(_decode_section(item, "appendix") for item in _expect_list(obj["appendices"], "appendices")),
            citations=tuple(_decode_citation(item) for item in _expect_list(obj["citations"], "citations")),
            sources=tuple(_decode_source(item) for item in _expect_list(obj["sources"], "sources")),
            revisions=tuple(_decode_revision(item) for item in _expect_list(obj["revisions"], "revisions")),
            reviews=tuple(_decode_review(item) for item in _expect_list(obj["reviews"], "reviews")),
            signatures=tuple(_decode_signature(item) for item in _expect_list(obj["signatures"], "signatures")),
            schema_version=version,
        )

    @classmethod
    def from_json(cls, value: str | bytes) -> "ArtifactDocument":
        if type(value) not in {str, bytes}:
            raise ArtifactModelError("artifact JSON must be exact text or bytes")
        try:
            parsed = json.loads(
                value,
                object_pairs_hook=_strict_json_object,
                parse_constant=_reject_json_constant,
            )
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ArtifactModelError("artifact JSON is invalid") from exc
        return ArtifactDocument.from_dict(parsed)


def _block_dict(block: Block) -> dict[str, Any]:
    block_type = _BLOCK_NAMES[type(block)]
    result: dict[str, Any] = {"type": block_type, "block_id": block.block_id}
    if type(block) is ParagraphBlock:
        result.update(text=block.text, citation_ids=list(block.citation_ids))
    elif type(block) is ListBlock:
        result.update(items=list(block.items), ordered=block.ordered, citation_ids=list(block.citation_ids))
    elif type(block) is TableBlock:
        result.update(columns=list(block.columns), rows=[list(row) for row in block.rows],
                      caption=block.caption, citation_ids=list(block.citation_ids))
    elif type(block) is FigureBlock:
        result.update(asset_id=block.asset_id, media_type=block.media_type,
                      alt_text=block.alt_text, caption=block.caption,
                      citation_ids=list(block.citation_ids))
    return result


def _section_dict(section: Section) -> dict[str, Any]:
    return {"section_id": section.section_id, "title": section.title, "level": section.level,
            "blocks": [_block_dict(block) for block in section.blocks]}


def _document_dict(doc: ArtifactDocument) -> dict[str, Any]:
    return {
        "schema_version": doc.schema_version,
        "artifact_id": doc.artifact_id,
        "metadata": {field.name: getattr(doc.metadata, field.name) for field in fields(doc.metadata)},
        "sections": [_section_dict(item) for item in doc.sections],
        "appendices": [_section_dict(item) for item in doc.appendices],
        "citations": [{"citation_id": item.citation_id, "source_id": item.source_id,
                       "span_ids": list(item.span_ids), "claim_ids": list(item.claim_ids),
                       "pinpoint": item.pinpoint} for item in doc.citations],
        "sources": [{field.name: getattr(item, field.name) for field in fields(item)} for item in doc.sources],
        "revisions": [{field.name: getattr(item, field.name) for field in fields(item)} for item in doc.revisions],
        "reviews": [{field.name: getattr(item, field.name) for field in fields(item)} for item in doc.reviews],
        "signatures": [{field.name: getattr(item, field.name) for field in fields(item)} for item in doc.signatures],
    }


def _decode_metadata(value: Any) -> DocumentMetadata:
    required = {field.name for field in fields(DocumentMetadata)} - {"subject"}
    obj = _expect_object(value, "metadata", required, {"subject"})
    return DocumentMetadata(**{name: obj.get(name, "") for name in required | {"subject"}})


def _decode_citation(value: Any) -> Citation:
    obj = _expect_object(value, "citation", {"citation_id", "source_id", "span_ids"}, {"claim_ids", "pinpoint"})
    return Citation(_nfc(obj["citation_id"], "citation_id"), _nfc(obj["source_id"], "source_id"),
                    tuple(_nfc(x, "span_id") for x in _expect_list(obj["span_ids"], "span_ids")),
                    tuple(_nfc(x, "claim_id") for x in _expect_list(obj.get("claim_ids", []), "claim_ids")),
                    _nfc(obj.get("pinpoint", ""), "pinpoint"))


def _decode_source(value: Any) -> SourceManifestEntry:
    obj = _expect_object(value, "source", {"source_id", "source_version_id", "content_hash"}, {"title", "canonical_uri"})
    return SourceManifestEntry(**{name: _nfc(obj.get(name, ""), name) for name in {field.name for field in fields(SourceManifestEntry)}})


def _decode_revision(value: Any) -> RevisionRecord:
    obj = _expect_object(value, "revision", {"revision_id", "sequence", "author_id", "created_at", "summary"}, {"parent_hash"})
    return RevisionRecord(_nfc(obj["revision_id"], "revision_id"), _exact_int(obj["sequence"], "sequence"),
                          _nfc(obj["author_id"], "author_id"), _nfc(obj["created_at"], "created_at"),
                          _nfc(obj["summary"], "summary"), _nfc(obj.get("parent_hash", ""), "parent_hash"))


def _decode_review(value: Any) -> ReviewRecord:
    names={field.name for field in fields(ReviewRecord)}
    obj=_expect_object(value, "review", names, set())
    return ReviewRecord(**{name:_nfc(obj[name], name) for name in names})


def _decode_signature(value: Any) -> SignatureRecord:
    required={field.name for field in fields(SignatureRecord)}-{"review_id"}
    obj=_expect_object(value, "signature", required, {"review_id"})
    return SignatureRecord(**{name:_nfc(obj.get(name, ""), name) for name in required|{"review_id"}})


def _decode_section(value: Any, label: str) -> Section:
    obj=_expect_object(value, label, {"section_id", "title", "level", "blocks"}, set())
    return Section(_nfc(obj["section_id"], "section_id"), _nfc(obj["title"], "title"),
                   _exact_int(obj["level"], "level"),
                   tuple(_decode_block(item) for item in _expect_list(obj["blocks"], "blocks")))


def _decode_block(value: Any) -> Block:
    if type(value) is not dict:
        raise ArtifactModelError("block must be an exact object")
    block_type=value.get("type")
    if type(block_type) is not str or block_type not in _BLOCK_TYPES:
        raise ArtifactModelError(f"unknown artifact block type: {block_type}")
    common={"type", "block_id"}
    if block_type=="paragraph":
        obj=_expect_object(value, "paragraph", common|{"text"}, {"citation_ids"})
        return ParagraphBlock(_nfc(obj["block_id"], "block_id"), _nfc(obj["text"], "text"),
                              tuple(_nfc(x,"citation_id") for x in _expect_list(obj.get("citation_ids",[]),"citation_ids")))
    if block_type=="list":
        obj=_expect_object(value,"list block",common|{"items"},{"ordered","citation_ids"})
        return ListBlock(_nfc(obj["block_id"],"block_id"), tuple(_nfc(x,"list item") for x in _expect_list(obj["items"],"items")),
                         _exact_bool(obj.get("ordered",False),"ordered"), tuple(_nfc(x,"citation_id") for x in _expect_list(obj.get("citation_ids",[]),"citation_ids")))
    if block_type=="table":
        obj=_expect_object(value,"table block",common|{"columns","rows"},{"caption","citation_ids"})
        return TableBlock(_nfc(obj["block_id"],"block_id"), tuple(_nfc(x,"column") for x in _expect_list(obj["columns"],"columns")),
                          tuple(tuple(_nfc(x,"cell") for x in _expect_list(row,"row")) for row in _expect_list(obj["rows"],"rows")),
                          _nfc(obj.get("caption",""),"caption"), tuple(_nfc(x,"citation_id") for x in _expect_list(obj.get("citation_ids",[]),"citation_ids")))
    if block_type=="figure":
        obj=_expect_object(value,"figure block",common|{"asset_id","media_type","alt_text"},{"caption","citation_ids"})
        return FigureBlock(_nfc(obj["block_id"],"block_id"),_nfc(obj["asset_id"],"asset_id"),_nfc(obj["media_type"],"media_type"),_nfc(obj["alt_text"],"alt_text"),
                           _nfc(obj.get("caption",""),"caption"),tuple(_nfc(x,"citation_id") for x in _expect_list(obj.get("citation_ids",[]),"citation_ids")))
    obj=_expect_object(value,"page break",common,set())
    return PageBreakBlock(_nfc(obj["block_id"],"block_id"))
