from flask import Flask, render_template, request, jsonify, send_from_directory
from pathlib import Path
import fitz  # PyMuPDF
import uuid
import re
import traceback

app = Flask(__name__)

BASE_DIR = Path(__file__).resolve().parent
UPLOAD_FOLDER = BASE_DIR / "uploads"
OUTPUT_FOLDER = BASE_DIR / "outputs"
EXTRACTED_FONTS_FOLDER = BASE_DIR / "extracted_fonts"

UPLOAD_FOLDER.mkdir(exist_ok=True)
OUTPUT_FOLDER.mkdir(exist_ok=True)
EXTRACTED_FONTS_FOLDER.mkdir(exist_ok=True)


REGULAR_FALLBACK_FONT_CANDIDATES = (
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    "/Library/Fonts/Arial Unicode.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/Library/Fonts/Arial.ttf",
    "/System/Library/Fonts/Supplemental/STIXTwoText.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
)

ITALIC_FALLBACK_FONT_CANDIDATES = (
    "/System/Library/Fonts/Supplemental/Times New Roman Italic.ttf",
    "/System/Library/Fonts/Supplemental/Arial Italic.ttf",
    "/System/Library/Fonts/Supplemental/STIXTwoText-Italic.ttf",
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    "/Library/Fonts/Arial Unicode.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Oblique.ttf",
)

FONT_FAMILY_FILE_CANDIDATES = {
    "times": {
        "regular": (
            "/System/Library/Fonts/Supplemental/Times New Roman.ttf",
            "/Library/Fonts/Times New Roman.ttf",
        ),
        "bold": (
            "/System/Library/Fonts/Supplemental/Times New Roman Bold.ttf",
            "/Library/Fonts/Times New Roman Bold.ttf",
        ),
        "italic": (
            "/System/Library/Fonts/Supplemental/Times New Roman Italic.ttf",
            "/Library/Fonts/Times New Roman Italic.ttf",
        ),
        "bold_italic": (
            "/System/Library/Fonts/Supplemental/Times New Roman Bold Italic.ttf",
            "/Library/Fonts/Times New Roman Bold Italic.ttf",
        ),
    },
    "arial": {
        "regular": (
            "/System/Library/Fonts/Supplemental/Arial.ttf",
            "/Library/Fonts/Arial.ttf",
        ),
        "bold": (
            "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
            "/Library/Fonts/Arial Bold.ttf",
        ),
        "italic": (
            "/System/Library/Fonts/Supplemental/Arial Italic.ttf",
            "/Library/Fonts/Arial Italic.ttf",
        ),
        "bold_italic": (
            "/System/Library/Fonts/Supplemental/Arial Bold Italic.ttf",
            "/Library/Fonts/Arial Bold Italic.ttf",
        ),
    },
    "courier": {
        "regular": (
            "/System/Library/Fonts/Supplemental/Courier New.ttf",
            "/Library/Fonts/Courier New.ttf",
        ),
        "bold": (
            "/System/Library/Fonts/Supplemental/Courier New Bold.ttf",
            "/Library/Fonts/Courier New Bold.ttf",
        ),
        "italic": (
            "/System/Library/Fonts/Supplemental/Courier New Italic.ttf",
            "/Library/Fonts/Courier New Italic.ttf",
        ),
        "bold_italic": (
            "/System/Library/Fonts/Supplemental/Courier New Bold Italic.ttf",
            "/Library/Fonts/Courier New Bold Italic.ttf",
        ),
    },
    "georgia": {
        "regular": (
            "/System/Library/Fonts/Supplemental/Georgia.ttf",
            "/Library/Fonts/Georgia.ttf",
        ),
        "bold": (
            "/System/Library/Fonts/Supplemental/Georgia Bold.ttf",
            "/Library/Fonts/Georgia Bold.ttf",
        ),
        "italic": (
            "/System/Library/Fonts/Supplemental/Georgia Italic.ttf",
            "/Library/Fonts/Georgia Italic.ttf",
        ),
        "bold_italic": (
            "/System/Library/Fonts/Supplemental/Georgia Bold Italic.ttf",
            "/Library/Fonts/Georgia Bold Italic.ttf",
        ),
    },
}


def first_existing_font(candidates):
    for font_path in candidates:
        path = Path(font_path)
        if path.exists():
            return path
    return None


def font_supports_text(font_path, text):
    try:
        font = fitz.Font(fontfile=str(font_path))
    except Exception:
        return False

    return font_object_supports_text(font, text)


def font_object_supports_text(font_obj, text):
    for char in text or "":
        if char.isspace():
            continue
        try:
            if not font_obj.has_glyph(ord(char)):
                return False
        except Exception:
            return False

    return True


def first_font_supporting_text(candidates, text):
    for font_path in candidates:
        path = Path(font_path)
        if path.exists() and font_supports_text(path, text):
            return path

    return first_existing_font(candidates)


def default_fallback_font(text=""):
    return first_font_supporting_text(REGULAR_FALLBACK_FONT_CANDIDATES, text)


def default_italic_fallback_font(text=""):
    return first_font_supporting_text(ITALIC_FALLBACK_FONT_CANDIDATES, text)


def font_style_key(is_bold, is_italic):
    if is_bold and is_italic:
        return "bold_italic"
    if is_bold:
        return "bold"
    if is_italic:
        return "italic"
    return "regular"


def fallback_style_order(is_bold, is_italic):
    requested = font_style_key(is_bold, is_italic)
    order = [requested]

    for style in ("regular", "bold", "italic", "bold_italic"):
        if style not in order:
            order.append(style)

    return order


def font_family_key(pdf_font_name):
    name = clean_font_name(pdf_font_name).lower()

    if "times" in name:
        return "times"
    if "arial" in name or "helvetica" in name:
        return "arial"
    if "courier" in name:
        return "courier"
    if "georgia" in name:
        return "georgia"

    return None


def matching_system_font_candidates(pdf_font_name, is_bold, is_italic):
    family_key = font_family_key(pdf_font_name)
    if family_key is None:
        return []

    family = FONT_FAMILY_FILE_CANDIDATES[family_key]
    candidates = []

    for style in fallback_style_order(is_bold, is_italic):
        candidates.extend(family.get(style, ()))

    return candidates


def select_font_for_replacement(
    doc,
    page,
    span,
    new_text,
    fallback_font=None,
    italic_fallback_font=None,
):
    font_name = span.get("font", "")
    flags = int(span.get("flags", 0))
    font_name_lower = font_name.lower()
    is_italic = "italic" in font_name_lower or "oblique" in font_name_lower or bool(flags & 2)
    is_bold = "bold" in font_name_lower or bool(flags & 16)

    for font_path in matching_system_font_candidates(font_name, is_bold, is_italic):
        path = Path(font_path)
        if not path.exists():
            continue

        try:
            font_obj = fitz.Font(fontfile=str(path))
        except Exception:
            continue

        if font_object_supports_text(font_obj, new_text):
            return {
                "font_obj": font_obj,
                "font_file": str(path),
            }

    original_font_name, original_font_buffer = find_font_data(doc, page, font_name)
    if original_font_buffer is not None:
        font_obj = create_font_object_from_buffer(original_font_buffer)
        if font_obj is not None and font_object_supports_text(font_obj, new_text):
            return {
                "font_obj": font_obj,
                "font_buffer": original_font_buffer,
                "font_resource_name": original_font_name or f"ORIG_{uuid.uuid4().hex[:8]}",
            }

    if is_italic:
        fallback_path = default_italic_fallback_font(new_text) or italic_fallback_font
    else:
        fallback_path = default_fallback_font(new_text) or fallback_font

    if fallback_path is None:
        return None

    try:
        font_obj = fitz.Font(fontfile=str(fallback_path))
    except Exception:
        return None

    return {
        "font_obj": font_obj,
        "font_file": str(fallback_path),
    }


def int_rgb_to_tuple(color_int):
    red = (color_int >> 16) & 255
    green = (color_int >> 8) & 255
    blue = color_int & 255
    return red / 255, green / 255, blue / 255


def clean_font_name(name):
    return name.split("+", 1)[-1] if name else ""


def normalize_text(value):
    return (value or "").replace("\u00a0", " ").strip()


def slugify_font_name(name):
    cleaned = clean_font_name(name)
    cleaned = re.sub(r"[^a-zA-Z0-9_\-]+", "_", cleaned)
    return cleaned.strip("_") or f"font_{uuid.uuid4().hex[:8]}"


def browser_safe_font_name(pdf_font_name):
    name = clean_font_name(pdf_font_name).lower()

    if "times" in name:
        return "Times New Roman, Times, serif"
    if "arial" in name or "helvetica" in name:
        return "Arial, Helvetica, sans-serif"
    if "courier" in name:
        return "Courier New, Courier, monospace"
    if "calibri" in name:
        return "Calibri, Arial, sans-serif"
    if "georgia" in name:
        return "Georgia, serif"

    return "Times New Roman, serif"


def spans_have_same_style(a, b):
    if clean_font_name(a.get("font", "")).lower() != clean_font_name(b.get("font", "")).lower():
        return False

    if abs(float(a.get("size", 0)) - float(b.get("size", 0))) > 0.25:
        return False

    if int(a.get("flags", 0)) != int(b.get("flags", 0)):
        return False

    if int(a.get("color", 0)) != int(b.get("color", 0)):
        return False

    return True


def spans_are_close(a, b, max_gap=3.0):
    gap = float(b["bbox"][0]) - float(a["bbox"][2])
    return gap <= max_gap


def extract_page_fonts(doc, page):
    font_map = {}

    for font in page.get_fonts(full=True):
        try:
            xref = font[0]
            base_font = clean_font_name(font[3] if len(font) > 3 else "")
            if not base_font:
                continue

            safe_name = slugify_font_name(base_font)
            extracted = doc.extract_font(xref)
            ext = extracted[1]
            font_buffer = extracted[3]

            if not font_buffer or not ext:
                font_map[base_font.lower()] = {
                    "webFontFamily": None,
                    "webFontUrl": None,
                    "extracted": False,
                }
                continue

            filename = f"{safe_name}.{ext}"
            out_path = EXTRACTED_FONTS_FOLDER / filename

            if not out_path.exists():
                with open(out_path, "wb") as f:
                    f.write(font_buffer)

            font_map[base_font.lower()] = {
                "webFontFamily": safe_name,
                "webFontUrl": f"/extracted_fonts/{filename}",
                "extracted": True,
            }

        except Exception:
            continue

    return font_map


def find_best_web_font(font_name, page_font_map):
    cleaned = clean_font_name(font_name).lower()

    if cleaned in page_font_map and page_font_map[cleaned]["extracted"]:
        return page_font_map[cleaned]

    for key, value in page_font_map.items():
        if cleaned == key or cleaned in key or key in cleaned:
            if value["extracted"]:
                return value

    return {
        "webFontFamily": None,
        "webFontUrl": None,
        "extracted": False,
    }


def build_unit_from_spans(page_index, spans, page_font_map):
    if not spans:
        return None

    full_text = "".join(span.get("text", "") for span in spans)
    if not normalize_text(full_text):
        return None

    x0 = min(span["bbox"][0] for span in spans)
    y0 = min(span["bbox"][1] for span in spans)
    x1 = max(span["bbox"][2] for span in spans)
    y1 = max(span["bbox"][3] for span in spans)

    dominant_span = max(
        spans,
        key=lambda s: len(normalize_text(s.get("text", ""))) or 1
    )

    font_name = dominant_span.get("font", "")
    flags = dominant_span.get("flags", 0)
    font_name_lower = font_name.lower()

    is_italic = (
        "italic" in font_name_lower
        or "oblique" in font_name_lower
        or bool(flags & 2)
    )

    is_bold = (
        "bold" in font_name_lower
        or bool(flags & 16)
    )

    web_font_info = find_best_web_font(font_name, page_font_map)

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
        "webFontFamily": web_font_info["webFontFamily"],
        "webFontUrl": web_font_info["webFontUrl"],
        "size": dominant_span.get("size", 12),
        "color": dominant_span.get("color", 0),
        "flags": flags,
        "origin": dominant_span.get("origin", [x0, y1]),
        "isItalic": is_italic,
        "isBold": is_bold,
        "maxX": None,
    }


def split_line_into_style_runs(page_index, line, page_font_map):
    spans = line.get("spans", [])
    if not spans:
        return []

    units = []
    current_group = []

    for span in spans:
        span_text = span.get("text", "")
        if span_text == "":
            continue

        if not current_group:
            current_group.append(span)
            continue

        prev_span = current_group[-1]

        if spans_have_same_style(prev_span, span) and spans_are_close(prev_span, span):
            current_group.append(span)
        else:
            unit = build_unit_from_spans(page_index, current_group, page_font_map)
            if unit:
                units.append(unit)
            current_group = [span]

    if current_group:
        unit = build_unit_from_spans(page_index, current_group, page_font_map)
        if unit:
            units.append(unit)

    return units


def extract_pdf_data(pdf_path):
    doc = fitz.open(pdf_path)
    pages_result = []
    font_faces = []
    seen_font_families = set()

    for page_index, page in enumerate(doc):
        page_font_map = extract_page_fonts(doc, page)
        units = []
        page_dict = page.get_text("dict")

        for block in page_dict.get("blocks", []):
            for line in block.get("lines", []):
                line_units = split_line_into_style_runs(page_index, line, page_font_map)

                for idx, unit in enumerate(line_units):
                    if idx < len(line_units) - 1:
                        next_unit = line_units[idx + 1]
                        unit["maxX"] = next_unit["x0"] - 2
                    else:
                        unit["maxX"] = page.rect.width - 5

                units.extend(line_units)

        for unit in units:
            if unit["webFontFamily"] and unit["webFontUrl"]:
                key = (unit["webFontFamily"], unit["webFontUrl"])
                if key not in seen_font_families:
                    seen_font_families.add(key)
                    font_faces.append({
                        "fontFamily": unit["webFontFamily"],
                        "url": unit["webFontUrl"],
                    })

        pages_result.append({
            "page": page_index,
            "width": page.rect.width,
            "height": page.rect.height,
            "units": units,
        })

    doc.close()
    return pages_result, font_faces


def find_span_for_rect(page, rect):
    text = page.get_text("dict")
    best_span = None
    best_score = 0.0

    for block in text.get("blocks", []):
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                span_rect = fitz.Rect(span["bbox"])
                intersection = span_rect & rect
                area = intersection.get_area()
                if area <= 0:
                    continue

                text_weight = sum(char.isalnum() for char in span.get("text", ""))
                score = area * max(text_weight, 1)

                if score > best_score:
                    best_score = score
                    best_span = span

    return best_span


def find_font_data(doc, page, span_font_name):
    """
    Try to match the span font to a real page font resource.
    Returns:
      (font_name_for_page_insert_font, font_buffer_or_none)
    """
    wanted = clean_font_name(span_font_name).lower()

    for font in page.get_fonts(full=True):
        xref = font[0]
        base_font = clean_font_name(font[3] if len(font) > 3 else "").lower()
        resource_name = font[4] if len(font) > 4 else "helv"

        if base_font == wanted or wanted in base_font or base_font in wanted:
            try:
                extracted = doc.extract_font(xref)
                font_buffer = extracted[3]
                if font_buffer:
                    return "REPLFONT", font_buffer
            except Exception:
                pass

            return resource_name or "helv", None

    return None, None


def create_font_object_from_buffer(font_buffer):
    try:
        return fitz.Font(fontbuffer=font_buffer)
    except Exception:
        return None


def create_font_object_from_file(font_path):
    try:
        return fitz.Font(fontfile=str(font_path))
    except Exception:
        return None


def text_fits_single_line(font_obj, text, fontsize, width_limit):
    if font_obj is None:
        return False

    try:
        text_width = font_obj.text_length(text, fontsize=fontsize)
        # небольшой запас на округление и визуальную погрешность
        return text_width <= max(width_limit - 1.5, 1)
    except Exception:
        return False


def replace_item_keep_style(
    doc,
    page,
    rect,
    new_text,
    max_x=None,
    fallback_font=None,
    italic_fallback_font=None,
    background=(1, 1, 1),
):
    span = find_span_for_rect(page, rect)
    if span is None:
        return False

    font_size = float(span.get("size", 12))
    color = int_rgb_to_tuple(span.get("color", 0))

    font_choice = select_font_for_replacement(
        doc=doc,
        page=page,
        span=span,
        new_text=new_text,
        fallback_font=fallback_font,
        italic_fallback_font=italic_fallback_font,
    )
    if font_choice is None:
        return False

    redraw_rect = fitz.Rect(rect)
    redraw_rect.y0 -= font_size * 0.20
    redraw_rect.y1 += font_size * 0.20

    text_right_limit = float(max_x) if max_x is not None else redraw_rect.x1 + font_size * 2.0
    text_right_limit = min(text_right_limit, page.rect.x1 - 2)
    available_width = max(text_right_limit - redraw_rect.x0, 1)

    baseline_y = span.get("origin", [redraw_rect.x0, redraw_rect.y1])[1]

    font_obj = font_choice["font_obj"]

    # Подбираем размер, чтобы влезло
    chosen_size = font_size
    fitted = False

    for _ in range(12):
        try:
            text_width = font_obj.text_length(new_text, fontsize=chosen_size)
            if text_width <= max(available_width - 1.5, 1):
                fitted = True
                break
        except Exception:
            return False

        chosen_size -= 0.5
        if chosen_size < 5:
            break

    # Не влезает — ничего не удаляем
    if not fitted:
        return False

    # Только теперь удаляем старый текст
    try:
        page.add_redact_annot(redraw_rect, fill=background)
        page.apply_redactions()
    except Exception:
        page.draw_rect(redraw_rect, color=background, fill=background, overlay=True)

    font_name = f"FALLBACK_{uuid.uuid4().hex[:8]}"

    try:
        if "font_buffer" in font_choice:
            page.insert_font(fontname=font_name, fontbuffer=font_choice["font_buffer"])
        else:
            page.insert_font(fontname=font_name, fontfile=font_choice["font_file"])

        page.insert_text(
            fitz.Point(redraw_rect.x0, baseline_y),
            new_text,
            fontname=font_name,
            fontsize=chosen_size,
            color=color,
            overlay=True,
        )
        return True
    except Exception:
        return False

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/upload", methods=["POST"])
def upload():
    file = request.files.get("pdf")
    if not file:
        return jsonify({"error": "Файл не загружен"}), 400

    if not file.filename.lower().endswith(".pdf"):
        return jsonify({"error": "Нужен PDF файл"}), 400

    file_id = str(uuid.uuid4())
    filename = f"{file_id}.pdf"
    pdf_path = UPLOAD_FOLDER / filename
    file.save(pdf_path)

    try:
        pages, font_faces = extract_pdf_data(pdf_path)
    except Exception as e:
        return jsonify({"error": f"Ошибка обработки PDF: {e}"}), 500

    return jsonify({
        "filename": filename,
        "pdfUrl": f"/uploads/{filename}",
        "pages": pages,
        "fontFaces": font_faces,
    })


@app.route("/save", methods=["POST"])
def save():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "Некорректный JSON"}), 400

    filename = data.get("filename")
    changes = data.get("changes", [])

    if not filename:
        return jsonify({"error": "Не передан filename"}), 400

    input_path = UPLOAD_FOLDER / filename
    if not input_path.exists():
        return jsonify({"error": "Исходный PDF не найден"}), 404

    output_name = f"edited_{filename}"
    output_path = OUTPUT_FOLDER / output_name

    fallback_font = default_fallback_font()
    italic_fallback_font = default_italic_fallback_font()

    try:
        doc = fitz.open(input_path)

        for change in changes:
            new_text = change.get("newText", "")
            old_text = change.get("oldText", "")

            if normalize_text(new_text) == normalize_text(old_text):
                continue

            page_index = int(change["page"])
            page = doc[page_index]

            rect = fitz.Rect(
                float(change["x0"]),
                float(change["y0"]),
                float(change["x1"]),
                float(change["y1"]),
            )

            max_x = change.get("maxX")
            if max_x is not None:
                max_x = float(max_x)

            ok = replace_item_keep_style(
                doc=doc,
                page=page,
                rect=rect,
                new_text=new_text,
                max_x=max_x,
                fallback_font=fallback_font,
                italic_fallback_font=italic_fallback_font,
                background=(1, 1, 1),
            )

            if not ok:
                print(
                    f"WARNING: could not replace text on page {page_index}: "
                    f"{old_text!r} -> {new_text!r}"
                )

        doc.save(output_path, garbage=4, deflate=True)
        doc.close()

    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": f"Ошибка сохранения PDF: {e}"}), 500

    return jsonify({
        "downloadUrl": f"/outputs/{output_name}"
    })


@app.route("/uploads/<path:filename>")
def uploaded_file(filename):
    return send_from_directory(UPLOAD_FOLDER, filename)


@app.route("/outputs/<path:filename>")
def output_file(filename):
    return send_from_directory(OUTPUT_FOLDER, filename, as_attachment=True)


@app.route("/extracted_fonts/<path:filename>")
def extracted_font_file(filename):
    return send_from_directory(EXTRACTED_FONTS_FOLDER, filename)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True, use_reloader=False)
