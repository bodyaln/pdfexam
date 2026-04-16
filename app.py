from flask import Flask, render_template, request, jsonify, Response, send_file
from pathlib import Path
import fitz
import uuid
import hashlib
import traceback
import re
import unicodedata
import io

app = Flask(__name__)

BASE_DIR = Path(__file__).resolve().parent
FONTS_DIR = BASE_DIR / "fonts"
FONTS_DIR.mkdir(exist_ok=True)

RUNTIME_FONTS = {}
PDF_STORE = {}
GENERATED_PDF_STORE = {}

BROWSER_FONT_EXTS = {"ttf", "otf", "woff", "woff2"}
BUILTIN_FONTNAMES = {
    "regular": "Times-Roman",
    "bold": "Times-Bold",
    "italic": "Times-Italic",
    "bolditalic": "Times-BoldItalic",
}
MIN_REPLACEMENT_FONT_SIZE = 5.0

PREFIX_ACCENT_MARKS = {
    "\u00b4": "\u0301",
    "\u02c7": "\u030c",
    "\u02c6": "\u0302",
    "`": "\u0300",
    "\u00a8": "\u0308",
}
PREFIX_ACCENT_RE = re.compile(
    f"([{re.escape(''.join(PREFIX_ACCENT_MARKS.keys()))}])([A-Za-z\u0131])"
)
POSTFIX_CARON_RE = re.compile(r"([dltDLT])[\u2019']")
DIACRITIC_SPAN_CHARS = set(PREFIX_ACCENT_MARKS.keys()) | {"\u2019", "'"}


def clean_font_name(name: str) -> str:
    return (name or "").split("+", 1)[-1]


def slugify(value: str) -> str:
    value = clean_font_name(value)
    value = re.sub(r"[^a-zA-Z0-9_-]+", "_", value)
    return value.strip("_") or f"font_{uuid.uuid4().hex[:8]}"


def normalize_pdf_unicode_text(value: str) -> str:
    text = (value or "").replace("\u00a0", " ")

    def compose_prefix_accent(match):
        accent, char = match.groups()
        if char == "\u0131":
            char = "i"
        return unicodedata.normalize("NFC", char + PREFIX_ACCENT_MARKS[accent])

    def compose_postfix_caron(match):
        return unicodedata.normalize("NFC", match.group(1) + "\u030c")

    text = PREFIX_ACCENT_RE.sub(compose_prefix_accent, text)
    text = POSTFIX_CARON_RE.sub(compose_postfix_caron, text)
    return unicodedata.normalize("NFC", text)


def normalize_text(value: str) -> str:
    return normalize_pdf_unicode_text(value).strip()


def is_diacritic_span(span) -> bool:
    stripped = (span.get("text", "") or "").strip()
    return bool(stripped) and all(ch in DIACRITIC_SPAN_CHARS for ch in stripped)


def font_traits(font_name: str, flags: int = 0):
    name = clean_font_name(font_name).lower()
    flags = int(flags or 0)

    is_bold = (
        "bold" in name
        or "cmbx" in name
        or bool(flags & 16)
    )
    is_italic = (
        "italic" in name
        or "oblique" in name
        or "cmti" in name
        or bool(flags & 2)
    )
    return is_bold, is_italic


def style_key(is_bold: bool, is_italic: bool) -> str:
    if is_bold and is_italic:
        return "bolditalic"
    if is_bold:
        return "bold"
    if is_italic:
        return "italic"
    return "regular"


def classify_family(font_name: str) -> str:
    name = clean_font_name(font_name).lower()

    if any(x in name for x in ["cm", "lmroman", "latinmodern", "cmbx", "cmr", "cmti", "ec"]):
        return "tex"
    if any(x in name for x in ["arial", "helvetica", "calibri", "sans"]):
        return "sans"
    if any(x in name for x in ["courier", "mono"]):
        return "mono"
    return "serif"


def browser_safe_font_name(pdf_font_name: str) -> str:
    family = classify_family(pdf_font_name)
    if family == "sans":
        return "Arial, Helvetica, sans-serif"
    if family == "mono":
        return "Courier New, Courier, monospace"
    return "Times New Roman, Times, serif"


def fallback_font_path(font_name: str, is_bold: bool, is_italic: bool) -> Path | None:
    family = classify_family(font_name)

    if family == "tex":
        if is_bold and is_italic:
            path = FONTS_DIR / "tex" / "lmroman10-bolditalic.otf"
        elif is_bold:
            path = FONTS_DIR / "tex" / "lmroman10-bold.otf"
        elif is_italic:
            path = FONTS_DIR / "tex" / "lmroman10-italic.otf"
        else:
            path = FONTS_DIR / "tex" / "lmroman10-regular.otf"
        return path if path.exists() else None

    if family == "sans":
        path = FONTS_DIR / "generic" / ("NotoSans-Bold.ttf" if is_bold else "NotoSans-Regular.ttf")
        return path if path.exists() else None

    if family == "mono":
        path = FONTS_DIR / "generic" / "NotoSansMono-Regular.ttf"
        return path if path.exists() else None

    path = FONTS_DIR / "generic" / ("NotoSerif-Bold.ttf" if is_bold else "NotoSerif-Regular.ttf")
    return path if path.exists() else None


def font_mimetype(ext: str) -> str:
    ext = (ext or "").lower()
    if ext == "otf":
        return "font/otf"
    if ext == "ttf":
        return "font/ttf"
    if ext == "woff":
        return "font/woff"
    if ext == "woff2":
        return "font/woff2"
    return "application/octet-stream"


def register_runtime_font(font_buffer: bytes, ext: str, font_name: str) -> str:
    digest = hashlib.sha256(font_buffer).hexdigest()[:20]
    ext = (ext or "otf").lower()
    font_id = f"{slugify(font_name)}_{digest}.{ext}"
    RUNTIME_FONTS[font_id] = {
        "buffer": font_buffer,
        "mimetype": font_mimetype(ext),
    }
    return f"/runtime_fonts/{font_id}"


def create_font_obj_from_buffer(font_buffer: bytes | None):
    if not font_buffer:
        return None
    try:
        return fitz.Font(fontbuffer=font_buffer)
    except Exception:
        return None


def font_supports_text(font_obj, text: str) -> bool:
    if font_obj is None:
        return False
    try:
        for ch in text or "":
            if ch.isspace():
                continue
            if not font_obj.has_glyph(ord(ch)):
                return False
        return True
    except Exception:
        return False


def load_fallback_font(font_name: str, is_bold: bool, is_italic: bool):
    path = fallback_font_path(font_name, is_bold, is_italic)
    if path is None:
        return None

    try:
        buffer = path.read_bytes()
        font_obj = create_font_obj_from_buffer(buffer)
        if font_obj is None:
            return None
        return {
            "buffer": buffer,
            "ext": path.suffix.lstrip(".").lower() or "ttf",
            "font_obj": font_obj,
            "path": str(path),
        }
    except Exception:
        return None


def extract_original_font_resource(doc, page, span_font_name: str):
    wanted = clean_font_name(span_font_name).lower()

    for font in page.get_fonts(full=True):
        xref = font[0]
        base_font = clean_font_name(font[3] if len(font) > 3 else "").lower()

        if base_font == wanted or wanted in base_font or base_font in wanted:
            try:
                extracted = doc.extract_font(xref)
                ext = (extracted[1] or "").lower()
                font_buffer = extracted[3]
                if font_buffer:
                    return {
                        "xref": xref,
                        "font_buffer": font_buffer,
                        "font_ext": ext,
                        "base_font": base_font,
                    }
            except Exception:
                pass

    return None


def choose_insert_font(doc, page, font_name: str, flags: int, text: str):
    text = normalize_pdf_unicode_text(text)
    is_bold, is_italic = font_traits(font_name, flags)

    resource = extract_original_font_resource(doc, page, font_name)
    if resource:
        font_obj = create_font_obj_from_buffer(resource["font_buffer"])
        if font_supports_text(font_obj, text):
            return {
                "mode": "buffer",
                "font_obj": font_obj,
                "font_buffer": resource["font_buffer"],
            }

    fallback = load_fallback_font(font_name, is_bold, is_italic)
    if fallback and font_supports_text(fallback["font_obj"], text):
        return {
            "mode": "buffer",
            "font_obj": fallback["font_obj"],
            "font_buffer": fallback["buffer"],
        }

    builtin_name = BUILTIN_FONTNAMES[style_key(is_bold, is_italic)]
    try:
        font_obj = fitz.Font(fontname=builtin_name)
        if font_supports_text(font_obj, text):
            return {
                "mode": "builtin",
                "font_obj": font_obj,
                "font_name": builtin_name,
            }
    except Exception:
        pass

    return None


def same_style(a, b) -> bool:
    if clean_font_name(a.get("font", "")).lower() != clean_font_name(b.get("font", "")).lower():
        return False

    if abs(float(a.get("size", 0)) - float(b.get("size", 0))) > 0.5:
        return False

    flags_a = int(a.get("flags", 0))
    flags_b = int(b.get("flags", 0))
    if (flags_a & 2) != (flags_b & 2):
        return False
    if (flags_a & 16) != (flags_b & 16):
        return False

    if int(a.get("color", 0)) != int(b.get("color", 0)):
        return False

    return True


def close_enough(a, b, max_gap=6.0) -> bool:
    return float(b["bbox"][0]) - float(a["bbox"][2]) <= max_gap


def int_rgb_to_tuple(color_int: int):
    r = (color_int >> 16) & 255
    g = (color_int >> 8) & 255
    b = color_int & 255
    return r / 255, g / 255, b / 255


def collect_page_font_faces(doc, page):
    font_map = {}

    for font in page.get_fonts(full=True):
        try:
            xref = font[0]
            base_font = clean_font_name(font[3] if len(font) > 3 else "")
            if not base_font:
                continue

            extracted = doc.extract_font(xref)
            ext = (extracted[1] or "").lower()
            font_buffer = extracted[3]

            is_bold, is_italic = font_traits(base_font)

            final_buffer = None
            final_ext = None

            if font_buffer and ext in BROWSER_FONT_EXTS:
                final_buffer = font_buffer
                final_ext = ext
            else:
                fallback = load_fallback_font(base_font, is_bold, is_italic)
                if fallback:
                    final_buffer = fallback["buffer"]
                    final_ext = fallback["ext"]

            if final_buffer:
                font_map[base_font.lower()] = {
                    "webFontFamily": slugify(base_font),
                    "webFontUrl": register_runtime_font(final_buffer, final_ext, base_font),
                    "fontStyle": "italic" if is_italic else "normal",
                    "fontWeight": "700" if is_bold else "400",
                }
            else:
                font_map[base_font.lower()] = {
                    "webFontFamily": None,
                    "webFontUrl": None,
                    "fontStyle": "italic" if is_italic else "normal",
                    "fontWeight": "700" if is_bold else "400",
                }
        except Exception:
            continue

    return font_map


def find_best_web_font(font_name: str, page_font_map: dict):
    cleaned = clean_font_name(font_name).lower()

    if cleaned in page_font_map:
        return page_font_map[cleaned]

    for key, value in page_font_map.items():
        if cleaned == key or cleaned in key or key in cleaned:
            return value

    return {
        "webFontFamily": None,
        "webFontUrl": None,
        "fontStyle": "normal",
        "fontWeight": "400",
    }


def build_unit_from_spans(page_index: int, spans: list, page_font_map: dict):
    if not spans:
        return None

    full_text = normalize_pdf_unicode_text("".join(span.get("text", "") for span in spans))
    if not normalize_text(full_text):
        return None

    x0 = min(span["bbox"][0] for span in spans)
    y0 = min(span["bbox"][1] for span in spans)
    x1 = max(span["bbox"][2] for span in spans)
    y1 = max(span["bbox"][3] for span in spans)

    dominant = max(
        spans,
        key=lambda s: len(normalize_text(s.get("text", ""))) or 1
    )

    font_name = dominant.get("font", "")
    flags = int(dominant.get("flags", 0))
    is_bold, is_italic = font_traits(font_name, flags)
    web_font = find_best_web_font(font_name, page_font_map)

    return {
        "id": str(uuid.uuid4()),
        "page": page_index,
        "text": full_text,
        "originalText": full_text,
        "x0": x0,
        "y0": y0,
        "x1": x1,
        "y1": y1,
        "font": font_name,
        "browserFont": browser_safe_font_name(font_name),
        "webFontFamily": web_font["webFontFamily"],
        "webFontUrl": web_font["webFontUrl"],
        "webFontStyle": web_font["fontStyle"],
        "webFontWeight": web_font["fontWeight"],
        "size": float(dominant.get("size", 12)),
        "color": int(dominant.get("color", 0)),
        "flags": flags,
        "isBold": is_bold,
        "isItalic": is_italic,
        "originY": float(dominant.get("origin", [x0, y1])[1]),
        "maxX": None,
    }


def split_line_into_units(page_index: int, line: dict, page_font_map: dict):
    spans = line.get("spans", [])
    if not spans:
        return []

    units = []
    current = []

    for span in spans:
        span_text = span.get("text", "")
        if span_text == "":
            continue

        if not current:
            current.append(span)
            continue

        prev = current[-1]

        if is_diacritic_span(span):
            current.append(span)
            continue

        if same_style(prev, span) and close_enough(prev, span):
            current.append(span)
        else:
            unit = build_unit_from_spans(page_index, current, page_font_map)
            if unit:
                units.append(unit)
            current = [span]

    if current:
        unit = build_unit_from_spans(page_index, current, page_font_map)
        if unit:
            units.append(unit)

    return units


def extract_pdf_data_from_bytes(pdf_bytes: bytes):
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    pages_result = []
    font_faces = []
    seen_faces = set()

    for page_index, page in enumerate(doc):
        page_font_map = collect_page_font_faces(doc, page)
        units = []
        page_dict = page.get_text("dict")

        for block in page_dict.get("blocks", []):
            for raw_line in block.get("lines", []):
                line_units = split_line_into_units(page_index, raw_line, page_font_map)

                line_bbox = None
                if "bbox" in raw_line:
                    line_bbox = list(raw_line["bbox"])

                for i, unit in enumerate(line_units):
                    if i < len(line_units) - 1:
                        unit["maxX"] = line_units[i + 1]["x0"] - 2
                    else:
                        unit["maxX"] = page.rect.width - 5

                    unit["lineBBox"] = line_bbox

                units.extend(line_units)

        for unit in units:
            if unit["webFontFamily"] and unit["webFontUrl"]:
                key = (
                    unit["webFontFamily"],
                    unit["webFontUrl"],
                    unit["webFontStyle"],
                    unit["webFontWeight"],
                )
                if key not in seen_faces:
                    seen_faces.add(key)
                    font_faces.append({
                        "fontFamily": unit["webFontFamily"],
                        "url": unit["webFontUrl"],
                        "fontStyle": unit["webFontStyle"],
                        "fontWeight": unit["webFontWeight"],
                    })

        pages_result.append({
            "page": page_index,
            "width": page.rect.width,
            "height": page.rect.height,
            "units": units,
        })

    doc.close()
    return pages_result, font_faces


def find_best_span_for_rect(page, rect: fitz.Rect):
    text = page.get_text("dict")
    best_span = None
    best_score = 0.0

    for block in text.get("blocks", []):
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                span_rect = fitz.Rect(span["bbox"])
                inter = span_rect & rect
                area = inter.get_area()
                if area <= 0:
                    continue

                score = area * max(sum(ch.isalnum() for ch in span.get("text", "")), 1)
                if score > best_score:
                    best_score = score
                    best_span = span

    return best_span


def build_erase_rect(old_rect: fitz.Rect, new_rect: fitz.Rect, font_size: float):
    erase_rect = fitz.Rect(
        min(old_rect.x0, new_rect.x0),
        min(old_rect.y0, new_rect.y0),
        max(old_rect.x1, new_rect.x1),
        max(old_rect.y1, new_rect.y1),
    )
    erase_rect.y0 -= font_size * 0.05
    erase_rect.y1 += font_size * 0.05
    return erase_rect


def erase_old_and_new_regions(page, old_rect: fitz.Rect, new_rect: fitz.Rect, font_size: float):
    erase_rect = build_erase_rect(old_rect, new_rect, font_size)
    try:
        page.add_redact_annot(erase_rect, fill=(1, 1, 1))
        page.apply_redactions()
    except Exception:
        page.draw_rect(erase_rect, color=(1, 1, 1), fill=(1, 1, 1), overlay=True)


def replace_text_keep_style(
    doc,
    page,
    rect,
    new_text,
    max_x=None,
    origin_y=None,
    font_name=None,
    flags=0,
    size=None,
    color_int=None,
    draw_x_override=None,
    current_x1_override=None,
):
    new_text = normalize_pdf_unicode_text(new_text)
    operation = build_text_operation(
        doc=doc,
        page=page,
        rect=rect,
        new_text=new_text,
        max_x=max_x,
        origin_y=origin_y,
        font_name=font_name,
        flags=flags,
        size=size,
        color_int=color_int,
        draw_x_override=draw_x_override,
        current_x1_override=current_x1_override,
    )

    if operation is None:
        return False

    try:
        page.add_redact_annot(operation["erase_rect"], fill=(1, 1, 1))
        page.apply_redactions()
    except Exception:
        page.draw_rect(operation["erase_rect"], color=(1, 1, 1), fill=(1, 1, 1), overlay=True)

    return insert_text_operation(page, operation)


def build_text_operation(
    doc,
    page,
    rect,
    new_text,
    max_x=None,
    origin_y=None,
    font_name=None,
    flags=0,
    size=None,
    color_int=None,
    draw_x_override=None,
    current_x1_override=None,
):
    new_text = normalize_pdf_unicode_text(new_text)
    span = find_best_span_for_rect(page, rect)

    if span is None and (font_name is None or size is None or color_int is None or origin_y is None):
        return None

    if span is not None:
        font_name = span.get("font", font_name or "")
        flags = int(span.get("flags", flags or 0))
        if size is None:
            size = float(span.get("size", 12))
        if color_int is None:
            color_int = int(span.get("color", 0))
        if origin_y is None:
            origin_y = float(span.get("origin", [rect.x0, rect.y1])[1])

    color = int_rgb_to_tuple(int(color_int))
    baseline_y = float(origin_y)
    font_size = float(size)

    selected_font = choose_insert_font(doc, page, font_name, flags, new_text)
    if selected_font is None:
        return None

    original_draw_x = float(rect.x0)
    draw_x = float(draw_x_override) if draw_x_override is not None else original_draw_x
    right_limit = float(max_x) if max_x is not None else page.rect.width - 5
    available_width = max(right_limit - draw_x, 1)

    try:
        final_width = selected_font["font_obj"].text_length(new_text, fontsize=font_size)
    except Exception:
        return None

    if final_width > available_width + 1.5:
        return None

    new_rect_x1 = draw_x + final_width + 2
    if current_x1_override is not None:
        new_rect_x1 = max(new_rect_x1, float(current_x1_override))
    new_rect_x1 = min(new_rect_x1, right_limit)

    old_rect = fitz.Rect(rect)
    new_rect = fitz.Rect(draw_x, rect.y0, new_rect_x1, rect.y1)
    erase_rect = build_erase_rect(old_rect, new_rect, font_size)

    return {
        "text": new_text,
        "draw_x": draw_x,
        "baseline_y": baseline_y,
        "font_size": font_size,
        "color": color,
        "selected_font": selected_font,
        "erase_rect": erase_rect,
    }


def insert_text_operation(page, operation):
    if operation["text"] == "":
        return True

    selected_font = operation["selected_font"]
    try:
        if selected_font["mode"] == "buffer":
            runtime_font_name = f"F_{uuid.uuid4().hex[:8]}"
            page.insert_font(fontname=runtime_font_name, fontbuffer=selected_font["font_buffer"])
        else:
            runtime_font_name = selected_font["font_name"]

        page.insert_text(
            fitz.Point(operation["draw_x"], operation["baseline_y"]),
            operation["text"],
            fontname=runtime_font_name,
            fontsize=operation["font_size"],
            color=operation["color"],
            overlay=True,
        )
        return True
    except Exception:
        return False


def move_block_keep_style(
    doc,
    page,
    rect,
    text,
    max_x=None,
    origin_y=None,
    font_name=None,
    flags=0,
    size=None,
    color_int=None,
    draw_x_override=None,
    current_x1_override=None,
):
    return replace_text_keep_style(
        doc=doc,
        page=page,
        rect=rect,
        new_text=text,
        max_x=max_x,
        origin_y=origin_y,
        font_name=font_name,
        flags=flags,
        size=size,
        color_int=color_int,
        draw_x_override=draw_x_override,
        current_x1_override=current_x1_override,
    )


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/pdf/open", methods=["POST"])
def open_pdf():
    file = request.files.get("pdf")
    if not file:
        return jsonify({"error": "Файл не загружен"}), 400

    if not file.filename.lower().endswith(".pdf"):
        return jsonify({"error": "Нужен PDF файл"}), 400

    pdf_bytes = file.read()
    if not pdf_bytes:
        return jsonify({"error": "Пустой PDF файл"}), 400

    file_id = str(uuid.uuid4())
    PDF_STORE[file_id] = {
        "filename": file.filename or f"{file_id}.pdf",
        "bytes": pdf_bytes,
    }

    try:
        pages, font_faces = extract_pdf_data_from_bytes(pdf_bytes)
        return jsonify({
            "filename": file_id,
            "pdfUrl": f"/api/pdf/source/{file_id}",
            "pages": pages,
            "fontFaces": font_faces,
        })
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": f"Ошибка обработки PDF: {e}"}), 500


@app.route("/api/pdf/apply", methods=["POST"])
def apply_pdf_changes():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "Некорректный JSON"}), 400

    file_id = data.get("filename")
    changes = data.get("changes", [])

    if not file_id:
        return jsonify({"error": "Не передан filename"}), 400

    item = PDF_STORE.get(file_id)
    if item is None:
        return jsonify({"error": "Исходный PDF не найден"}), 404

    try:
        doc = fitz.open(stream=item["bytes"], filetype="pdf")
        operations_by_page = {}

        for change in changes:
            old_text = normalize_pdf_unicode_text(change.get("oldText", ""))
            new_text = normalize_pdf_unicode_text(
                (change.get("newText", "") or "").replace("\n", " ")
            )

            page = doc[int(change["page"])]
            rect = fitz.Rect(
                float(change["x0"]),
                float(change["y0"]),
                float(change["x1"]),
                float(change["y1"]),
            )

            max_x = float(change["maxX"]) if change.get("maxX") is not None else None
            draw_x = float(change["drawX"]) if change.get("drawX") is not None else None
            current_x1 = float(change["currentX1"]) if change.get("currentX1") is not None else None
            moved_only = bool(change.get("movedOnly", False))

            if normalize_text(old_text) == normalize_text(new_text) and draw_x is None and not moved_only:
                continue

            operation = build_text_operation(
                doc=doc,
                page=page,
                rect=rect,
                new_text=new_text,
                max_x=max_x,
                origin_y=float(change["originY"]) if change.get("originY") is not None else None,
                font_name=change.get("font"),
                flags=int(change.get("flags", 0)),
                size=float(change["size"]) if change.get("size") is not None else None,
                color_int=int(change["color"]) if change.get("color") is not None else None,
                draw_x_override=draw_x,
                current_x1_override=current_x1,
            )

            if operation is None:
                print("WARNING: could not apply change:", old_text, "->", new_text)
                continue

            operation["page_index"] = int(change["page"])
            operations_by_page.setdefault(operation["page_index"], []).append(operation)

        for page_index, operations in operations_by_page.items():
            page = doc[page_index]
            redaction_ok = True

            try:
                for operation in operations:
                    page.add_redact_annot(operation["erase_rect"], fill=(1, 1, 1))
                page.apply_redactions()
            except Exception:
                redaction_ok = False

            if not redaction_ok:
                for operation in operations:
                    page.draw_rect(
                        operation["erase_rect"],
                        color=(1, 1, 1),
                        fill=(1, 1, 1),
                        overlay=True,
                    )

            operations.sort(key=lambda item: (item["baseline_y"], item["draw_x"]))
            for operation in operations:
                if not insert_text_operation(page, operation):
                    print("WARNING: could not insert text:", operation["text"])

        out_buffer = io.BytesIO()
        doc.save(out_buffer, garbage=4, deflate=True)
        doc.close()
        out_buffer.seek(0)

        output_id = str(uuid.uuid4())
        GENERATED_PDF_STORE[output_id] = {
            "filename": f"edited_{item['filename']}",
            "bytes": out_buffer.getvalue(),
        }

        return jsonify({
            "downloadUrl": f"/api/pdf/download/{output_id}"
        })
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": f"Ошибка сохранения PDF: {e}"}), 500


@app.route("/api/pdf/source/<file_id>")
def pdf_source(file_id):
    item = PDF_STORE.get(file_id)
    if item is None:
        return jsonify({"error": "PDF не найден"}), 404

    return send_file(
        io.BytesIO(item["bytes"]),
        mimetype="application/pdf",
        as_attachment=False,
        download_name=item["filename"],
    )


@app.route("/api/pdf/download/<output_id>")
def pdf_download(output_id):
    item = GENERATED_PDF_STORE.get(output_id)
    if item is None:
        return jsonify({"error": "Готовый PDF не найден"}), 404

    return send_file(
        io.BytesIO(item["bytes"]),
        mimetype="application/pdf",
        as_attachment=True,
        download_name=item["filename"],
    )


@app.route("/runtime_fonts/<path:font_id>")
def runtime_font_file(font_id):
    item = RUNTIME_FONTS.get(font_id)
    if item is None:
        return jsonify({"error": "Шрифт не найден"}), 404

    return Response(
        item["buffer"],
        mimetype=item["mimetype"],
        headers={"Cache-Control": "no-store"},
    )


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5001, debug=True, use_reloader=False)
