"""Shared safety and determinism helpers for artifact renderers."""
from __future__ import annotations

import io
import struct
import zipfile
import zlib
from xml.etree import ElementTree

from hybridagent.artifacts.models import ArtifactDocument, FigureBlock
from hybridagent.artifacts.validation import validate_or_raise

_FIXED_ZIP_TIME = (1980, 1, 1, 0, 0, 0)
MAX_ASSET_BYTES = 25 * 1024 * 1024
MAX_TOTAL_ASSET_BYTES = 100 * 1024 * 1024
MAX_DECODED_IMAGE_BYTES = 100 * 1024 * 1024
MAX_IMAGE_PIXELS = 25_000_000
MAX_IMAGE_DIMENSION = 100_000
_WINDOWS_RESERVED = frozenset(
    {
        "con",
        "prn",
        "aux",
        "nul",
        *(f"com{index}" for index in range(1, 10)),
        *(f"lpt{index}" for index in range(1, 10)),
    }
)


class MissingArtifactBackendError(RuntimeError):
    """An explicitly requested optional artifact backend is unavailable."""


class ArtifactRenderError(ValueError):
    """An artifact could not be rendered safely."""


def portable_member_path(path: str) -> bool:
    if type(path) is not str or not path or path.startswith(("/", "\\")) or "\\" in path:
        return False
    for part in path.split("/"):
        if (
            not part
            or part in {".", ".."}
            or part.endswith((" ", "."))
            or ":" in part
            or any(ord(char) < 32 for char in part)
            or part.split(".", 1)[0].rstrip(" .").casefold() in _WINDOWS_RESERVED
        ):
            return False
    return True


def require_backend(module: str, package: str) -> object:
    try:
        return __import__(module)
    except ImportError as exc:
        raise MissingArtifactBackendError(
            f"{package} is required for this renderer; from a Praxis source checkout run "
            'pip install -e ".[artifacts]"'
        ) from exc


def figure_ids(document: ArtifactDocument) -> set[str]:
    return {
        block.asset_id
        for section in (*document.sections, *document.appendices)
        for block in section.blocks
        if type(block) is FigureBlock
    }


def checked_assets(
    document: ArtifactDocument, assets: dict[str, bytes] | None
) -> dict[str, bytes]:
    validate_or_raise(document)
    if assets is None:
        assets = {}
    if type(assets) is not dict:
        raise ArtifactRenderError("figure assets must be an exact dictionary")
    result: dict[str, bytes] = {}
    total = 0
    for key, value in assets.items():
        if type(key) is not str or type(value) is not bytes:
            raise ArtifactRenderError("asset keys and payloads must be exact text and bytes")
        if not portable_member_path(f"assets/{key}") or "/" in key:
            raise ArtifactRenderError("asset ID is not a safe portable name")
        if len(value) > MAX_ASSET_BYTES:
            raise ArtifactRenderError("figure asset exceeds the per-asset size limit")
        total += len(value)
        if total > MAX_TOTAL_ASSET_BYTES:
            raise ArtifactRenderError("figure assets exceed the total size limit")
        result[key] = value
    missing = sorted(figure_ids(document) - result.keys())
    if missing:
        raise ArtifactRenderError(f"figure assets are missing: {missing}")
    expected_kinds = {
        "image/png": "png",
        "image/jpeg": "jpeg",
        "image/svg+xml": "svg",
    }
    for section in (*document.sections, *document.appendices):
        for block in section.blocks:
            if type(block) is not FigureBlock:
                continue
            try:
                actual_kind = image_kind(result[block.asset_id])
            except ArtifactRenderError as exc:
                raise ArtifactRenderError(
                    f"figure asset does not match its declared media type: {block.asset_id}"
                ) from exc
            if actual_kind != expected_kinds[block.media_type]:
                raise ArtifactRenderError(
                    f"figure asset does not match its declared media type: {block.asset_id}"
                )
    return result


def normalize_zip_package(data: bytes) -> bytes:
    """Normalize ZIP member order, metadata, and compression for stable Office output."""
    source = io.BytesIO(data)
    target = io.BytesIO()
    try:
        with zipfile.ZipFile(source, "r") as archive:
            names = archive.namelist()
            if len(names) != len(set(names)):
                raise ArtifactRenderError("generated package contains duplicate members")
            if len(names) != len({name.casefold() for name in names}):
                raise ArtifactRenderError("generated package member paths collide")
            if any(not portable_member_path(name) for name in names):
                raise ArtifactRenderError("generated package contains a nonportable member path")
            members = [(name, archive.read(name)) for name in sorted(names)]
    except (OSError, zipfile.BadZipFile, RuntimeError, NotImplementedError, zlib.error) as exc:
        raise ArtifactRenderError("generated Office package is invalid") from exc
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, payload in members:
            info = zipfile.ZipInfo(name, _FIXED_ZIP_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 0
            info.external_attr = 0o600 << 16
            archive.writestr(info, payload, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    return target.getvalue()


def _png_rows(
    width: int, height: int, bits_per_pixel: int, interlace: int
) -> list[tuple[int, int]]:
    if interlace == 0:
        return [(height, (width * bits_per_pixel + 7) // 8)]
    passes = (
        (0, 0, 8, 8),
        (4, 0, 8, 8),
        (0, 4, 4, 8),
        (2, 0, 4, 4),
        (0, 2, 2, 4),
        (1, 0, 2, 2),
        (0, 1, 1, 2),
    )
    rows: list[tuple[int, int]] = []
    for x_start, y_start, x_step, y_step in passes:
        pass_width = max(0, (width - x_start + x_step - 1) // x_step)
        pass_height = max(0, (height - y_start + y_step - 1) // y_step)
        if pass_width and pass_height:
            rows.append((pass_height, (pass_width * bits_per_pixel + 7) // 8))
    return rows


def _validate_png(payload: bytes) -> None:
    if not payload.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ArtifactRenderError("invalid PNG signature")
    offset = 8
    ihdr: bytes | None = None
    palette: bytes | None = None
    idat: list[bytes] = []
    idat_finished = False
    saw_iend = False
    while offset < len(payload):
        if len(payload) - offset < 12:
            raise ArtifactRenderError("PNG contains a truncated chunk")
        length = struct.unpack(">I", payload[offset:offset + 4])[0]
        chunk_type = payload[offset + 4:offset + 8]
        end = offset + 12 + length
        if end > len(payload):
            raise ArtifactRenderError("PNG contains a truncated chunk")
        data = payload[offset + 8:offset + 8 + length]
        expected_crc = struct.unpack(">I", payload[offset + 8 + length:end])[0]
        if zlib.crc32(chunk_type + data) & 0xFFFFFFFF != expected_crc:
            raise ArtifactRenderError("PNG chunk checksum is invalid")
        if len(chunk_type) != 4 or not all(
            65 <= byte <= 90 or 97 <= byte <= 122 for byte in chunk_type
        ):
            raise ArtifactRenderError("PNG chunk type is invalid")
        if ihdr is None and chunk_type != b"IHDR":
            raise ArtifactRenderError("PNG IHDR must be the first chunk")
        if chunk_type == b"IHDR":
            if ihdr is not None or length != 13:
                raise ArtifactRenderError("PNG IHDR is invalid")
            ihdr = data
        elif chunk_type == b"PLTE":
            if palette is not None or idat or not 0 < length <= 768 or length % 3:
                raise ArtifactRenderError("PNG palette is invalid")
            palette = data
        elif chunk_type == b"IDAT":
            if idat_finished:
                raise ArtifactRenderError("PNG IDAT chunks must be consecutive")
            idat.append(data)
        elif chunk_type == b"IEND":
            if length != 0 or not idat or end != len(payload):
                raise ArtifactRenderError("PNG IEND is invalid")
            saw_iend = True
            offset = end
            break
        else:
            if idat:
                idat_finished = True
            if 65 <= chunk_type[0] <= 90:
                raise ArtifactRenderError("PNG contains an unknown critical chunk")
        offset = end
    if ihdr is None or not saw_iend or offset != len(payload):
        raise ArtifactRenderError("PNG structure is incomplete")

    width, height, bit_depth, color_type, compression, filter_method, interlace = struct.unpack(
        ">IIBBBBB", ihdr
    )
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}
    allowed_depths = {
        0: {1, 2, 4, 8, 16},
        2: {8, 16},
        3: {1, 2, 4, 8},
        4: {8, 16},
        6: {8, 16},
    }
    if (
        not width
        or not height
        or width > MAX_IMAGE_DIMENSION
        or height > MAX_IMAGE_DIMENSION
        or width * height > MAX_IMAGE_PIXELS
        or color_type not in channels
        or bit_depth not in allowed_depths[color_type]
        or compression != 0
        or filter_method != 0
        or interlace not in {0, 1}
        or (color_type == 3 and palette is None)
        or (color_type in {0, 4} and palette is not None)
    ):
        raise ArtifactRenderError("PNG image header is invalid")
    rows = _png_rows(width, height, channels[color_type] * bit_depth, interlace)
    expected_size = sum(row_count * (row_bytes + 1) for row_count, row_bytes in rows)
    if expected_size > MAX_DECODED_IMAGE_BYTES:
        raise ArtifactRenderError("PNG decoded image exceeds the size limit")
    decoder = zlib.decompressobj()
    try:
        decoded = decoder.decompress(b"".join(idat), expected_size + 1)
    except zlib.error as exc:
        raise ArtifactRenderError("PNG image data is invalid") from exc
    if (
        len(decoded) != expected_size
        or not decoder.eof
        or decoder.unconsumed_tail
        or decoder.unused_data
    ):
        raise ArtifactRenderError("PNG image data is incomplete or oversized")
    cursor = 0
    for row_count, row_bytes in rows:
        for _ in range(row_count):
            if decoded[cursor] > 4:
                raise ArtifactRenderError("PNG row filter is invalid")
            cursor += row_bytes + 1


def _validate_jpeg(payload: bytes) -> None:
    if not payload.startswith(b"\xff\xd8"):
        raise ArtifactRenderError("invalid JPEG signature")
    offset = 2
    saw_frame = False
    saw_scan = False
    frame_component_ids: set[int] | None = None
    frame_markers = {
        *range(0xC0, 0xC4),
        *range(0xC5, 0xC8),
        *range(0xC9, 0xCC),
        *range(0xCD, 0xD0),
    }
    while offset < len(payload):
        if payload[offset] != 0xFF:
            raise ArtifactRenderError("JPEG marker stream is invalid")
        while offset < len(payload) and payload[offset] == 0xFF:
            offset += 1
        if offset >= len(payload):
            raise ArtifactRenderError("JPEG marker is truncated")
        marker = payload[offset]
        offset += 1
        if marker == 0xD9:
            if not saw_frame or not saw_scan or offset != len(payload):
                raise ArtifactRenderError("JPEG image is incomplete or has trailing bytes")
            return
        if marker in {0x00, 0x01, 0xD8, *range(0xD0, 0xD8)}:
            raise ArtifactRenderError("JPEG contains an invalid standalone marker")
        if offset + 2 > len(payload):
            raise ArtifactRenderError("JPEG segment length is truncated")
        length = struct.unpack(">H", payload[offset:offset + 2])[0]
        if length < 2 or offset + length > len(payload):
            raise ArtifactRenderError("JPEG segment is truncated")
        data = payload[offset + 2:offset + length]
        offset += length
        if marker in frame_markers:
            if saw_scan or frame_component_ids is not None:
                raise ArtifactRenderError("JPEG frame header ordering is invalid")
            if len(data) < 6:
                raise ArtifactRenderError("JPEG frame header is truncated")
            height, width, components = struct.unpack(">HHB", data[1:6])
            if (
                not width
                or not height
                or width > MAX_IMAGE_DIMENSION
                or height > MAX_IMAGE_DIMENSION
                or width * height > MAX_IMAGE_PIXELS
                or not components
                or len(data) != 6 + 3 * components
            ):
                raise ArtifactRenderError("JPEG frame header is invalid")
            component_data = data[6:]
            component_ids = {
                component_data[index * 3] for index in range(components)
            }
            sampling_factors = {
                component_data[index * 3 + 1] for index in range(components)
            }
            quantization_tables = {
                component_data[index * 3 + 2] for index in range(components)
            }
            if (
                len(component_ids) != components
                or any(
                    factor >> 4 not in range(1, 5) or factor & 0x0F not in range(1, 5)
                    for factor in sampling_factors
                )
                or any(table > 3 for table in quantization_tables)
            ):
                raise ArtifactRenderError("JPEG frame header is invalid")
            frame_component_ids = component_ids
            saw_frame = True
        if marker == 0xDA:
            if frame_component_ids is None or not data:
                raise ArtifactRenderError("JPEG scan header is invalid")
            components = data[0]
            if not components or len(data) != 1 + 2 * components + 3:
                raise ArtifactRenderError("JPEG scan header is invalid")
            scan_ids = [data[1 + index * 2] for index in range(components)]
            table_selectors = [data[2 + index * 2] for index in range(components)]
            spectral_start, spectral_end, approximation = data[-3:]
            if (
                len(set(scan_ids)) != components
                or not set(scan_ids).issubset(frame_component_ids)
                or any(selector >> 4 > 3 or selector & 0x0F > 3 for selector in table_selectors)
                or spectral_start > spectral_end
                or spectral_end > 63
                or approximation >> 4 > 13
                or approximation & 0x0F > 13
            ):
                raise ArtifactRenderError("JPEG scan header is invalid")
            saw_scan = True
            scan_offset = offset
            entropy_bytes = 0
            while scan_offset < len(payload):
                marker_start = payload.find(b"\xff", scan_offset)
                if marker_start < 0 or marker_start + 1 >= len(payload):
                    raise ArtifactRenderError("JPEG scan is unterminated")
                entropy_bytes += marker_start - scan_offset
                marker_end = marker_start + 1
                while marker_end < len(payload) and payload[marker_end] == 0xFF:
                    marker_end += 1
                if marker_end >= len(payload):
                    raise ArtifactRenderError("JPEG scan is unterminated")
                next_byte = payload[marker_end]
                if next_byte == 0x00:
                    entropy_bytes += 1
                    scan_offset = marker_end + 1
                    continue
                if 0xD0 <= next_byte <= 0xD7:
                    scan_offset = marker_end + 1
                    continue
                if entropy_bytes == 0:
                    raise ArtifactRenderError("JPEG scan contains no entropy data")
                offset = marker_start
                break
    raise ArtifactRenderError("JPEG image is missing its end marker")


def _validate_svg(payload: bytes) -> None:
    lowered = payload.lower()
    if b"<!doctype" in lowered or b"<!entity" in lowered:
        raise ArtifactRenderError("SVG document types and entities are forbidden")
    try:
        root = ElementTree.fromstring(payload)
    except (ElementTree.ParseError, UnicodeDecodeError, ValueError) as exc:
        raise ArtifactRenderError("SVG XML is invalid") from exc
    if type(root.tag) is not str:
        raise ArtifactRenderError("SVG root element is invalid")
    if root.tag.startswith("{"):
        namespace, _, local_name = root.tag[1:].partition("}")
    else:
        namespace, local_name = "", root.tag
    if local_name != "svg" or namespace not in {"", "http://www.w3.org/2000/svg"}:
        raise ArtifactRenderError("SVG root element is invalid")


def image_kind(payload: bytes) -> str:
    if type(payload) is not bytes:
        raise ArtifactRenderError("figure asset payload must be exact bytes")
    if payload.startswith(b"\x89PNG\r\n\x1a\n"):
        _validate_png(payload)
        return "png"
    if payload.startswith(b"\xff\xd8"):
        _validate_jpeg(payload)
        return "jpeg"
    if payload.lstrip().startswith((b"<", b"\xef\xbb\xbf<")):
        _validate_svg(payload)
        return "svg"
    raise ArtifactRenderError("figure asset is not a recognized PNG, JPEG, or SVG")
