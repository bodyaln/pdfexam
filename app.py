from flask import Flask, render_template, request, jsonify, send_from_directory
from pathlib import Path
import fitz  # PyMuPDF
import uuid
import re
import traceback
import unicodedata
import urllib.request
import os

app = Flask(__name__)

BASE_DIR = Path(__file__).resolve().parent
UPLOAD_FOLDER = BASE_DIR / "uploads"
OUTPUT_FOLDER = BASE_DIR / "outputs"
EXTRACTED_FONTS_FOLDER = BASE_DIR / "extracted_fonts"
PROJECT_FONTS_FOLDER = BASE_DIR / "fonts"
FONT_CACHE_FOLDER = BASE_DIR / ".font_cache"

UPLOAD_FOLDER.mkdir(exist_ok=True)
OUTPUT_FOLDER.mkdir(exist_ok=True)
EXTRACTED_FONTS_FOLDER.mkdir(exist_ok=True)
PROJECT_FONTS_FOLDER.mkdir(exist_ok=True)
FONT_CACHE_FOLDER.mkdir(exist_ok=True)

BROWSER_SUPPORTED_FONT_EXTENSIONS = {"ttf", "otf", "woff", "woff2"}

# ---- Диакритика / нормализация ----

PREFIX_ACCENT_MARKS = {
    "\u00b4": "\u0301",  # acute: ´y -> ý
    "\u02c7": "\u030c",  # caron: ˇs -> š
    "\u02c6": "\u0302",  # circumflex: ˆo -> ô
    "`": "\u0300",
    "\u00a8": "\u0308",
}
PREFIX_ACCENT_RE = re.compile(
    f"([{re.escape(''.join(PREFIX_ACCENT_MARKS))}])([A-Za-z\u0131])"
)
POSTFIX_CARON_RE = re.compile(r"([dltDLT])[\u2019']")

# Для некоторых TeX-like шрифтов
TEX_FONT_ACCENT_REPLACEMENTS = {
    "á": "\u00b4a",
    "Á": "\u00b4A",
    "é": "\u00b4e",
    "É": "\u00b4E",
    "í": "\u00b4\u0131",
    "Í": "\u00b4I",
    "ó": "\u00b4o",
    "Ó": "\u00b4O",
    "ú": "\u00b4u",
    "Ú": "\u00b4U",
    "ý": "\u00b4y",
    "Ý": "\u00b4Y",
    "ĺ": "\u00b4l",
    "Ĺ": "\u00b4L",
    "ŕ": "\u00b4r",
    "Ŕ": "\u00b4R",
    "č": "\u02c7c",
    "Č": "\u02c7C",
    "ď": "d\u2019",
    "Ď": "D\u2019",
    "ľ": "l\u2019",
    "Ľ": "L\u2019",
    "ň": "\u02c7n",
    "Ň": "\u02c7N",
    "š": "\u02c7s",
    "Š": "\u02c7S",
    "ť": "t\u2019",
    "Ť": "T\u2019",
    "ž": "\u02c7z",
    "Ž": "\u02c7Z",
    "ô": "\u02c6o",
    "Ô": "\u02c6O",
    "ä": "\u00a8a",
    "Ä": "\u00a8A",
}


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


def clean_font_name(name: str) -> str:
    return name.split("+", 1)[-1] if name else ""


def slugify_font_name(name: str) -> str:
    cleaned = clean_font_name(name)
    cleaned = re.sub(r"[^a-zA-Z0-9_\-]+", "_", cleaned)
    return cleaned.strip("_") or f"font_{uuid.uuid4().hex[:8]}"


def int_rgb_to_tuple(color_int: int):
    red = (color_int >> 16) & 255
    green = (color_int >> 8) & 255
    blue = color_int & 255
    return red / 255, green / 255, blue / 255


# ---- Определение стиля / семейства ----

def font_traits(font_name, flags=0):
    name = clean_font_name(font_name).lower()
    flags = int(flags or 0)

    is_italic = (
        "italic" in name
        or "oblique" in name
        or "cmti" in name
        or bool(flags & 2)
    )
    is_bold = (
        "bold" in name
        or "cmbx" in name
        or bool(flags & 16)
    )
    return is_bold, is_italic


def font_style_key(is_bold, is_italic):
    if is_bold and is_italic:
        return "bolditalic"
    if is_bold:
        return "bold"
    if is_italic:
        return "italic"
    return "regular"


def is_tex_like_font(font_name):
    name = clean_font_name(font_name).lower()
    return (
        name.startswith("cm")
        or name.startswith("lm")
        or "computer modern" in name
        or "latinmodern" in name
    )


def encode_text_for_original_font(font_name, text):
    text = normalize_pdf_unicode_text(text)
    if not is_tex_like_font(font_name):
        return text
    return "".join(TEX_FONT_ACCENT_REPLACEMENTS.get(ch, ch) for ch in text)


def classify_font_family(font_name):
    name = clean_font_name(font_name).lower()

    if (
        is_tex_like_font(name)
        or "times" in name
        or "georgia" in name
        or "serif" in name
        or "stix" in name
    ):
        return "serif"

    if "courier" in name or "mono" in name:
        return "mono"

    if "arial" in name or "helvetica" in name or "calibri" in name or "sans" in name:
        return "sans"

    return "serif"


def browser_safe_font_name(pdf_font_name):
    family = classify_font_family(pdf_font_name)
    if family == "mono":
        return "Courier New, Courier, monospace"
    if family == "sans":
        return "Arial, Helvetica, sans-serif"
    return "Times New Roman, Times, serif"


# ---- Проектные / кэшированные fallback-шрифты ----

PROJECT_FONT_FILES = {
    "serif": {
        "regular": "serif-regular.ttf",
        "italic": "serif-italic.ttf",
        "bold": "serif-bold.ttf",
        "bolditalic": "serif-bolditalic.ttf",
    },
    "sans": {
        "regular": "sans-regular.ttf",
        "italic": "sans-italic.ttf",
        "bold": "sans-bold.ttf",
        "bolditalic": "sans-bolditalic.ttf",
    },
    "mono": {
        "regular": "mono-regular.ttf",
        "italic": "mono-italic.ttf",
        "bold": "mono-bold.ttf",
        "bolditalic": "mono-bolditalic.ttf",
    },
}

# Можно передать URL через env, чтобы сервер сам скачал шрифты в кэш
# Например:
# PDFEDIT_SERIF_REGULAR_URL
# PDFEDIT_SERIF_ITALIC_URL
# PDFEDIT_SERIF_BOLD_URL
# PDFEDIT_SERIF_BOLDITALIC_URL
def env_font_url(family: str, style: str) -> str | None:
    key = f"PDFEDIT_{family.upper()}_{style.upper()}_URL"
    return os.getenv(key)


def get_project_font_path(family: str, style: str) -> Path:
    filename = PROJECT_FONT_FILES[family][style]
    return PROJECT_FONTS_FOLDER / filename


def get_cached_font_path(family: str, style: str) -> Path:
    filename = PROJECT_FONT_FILES[family][style]
    return FONT_CACHE_FOLDER / filename


def ensure_cached_font(family: str, style: str) -> Path | None:
    cached = get_cached_font_path(family, style)
    if cached.exists() and cached.stat().st_size > 0:
        return cached

    url = env_font_url(family, style)
    if not url:
        return None

    try:
        urllib.request.urlretrieve(url, cached)
        if cached.exists() and cached.stat().st_size > 0:
            return cached
    except Exception:
        pass

    return None


def resolve_deployed_font(family: str, style: str) -> Path | None:
    # 1) шрифт из проекта
    project_font = get_project_font_path(family, style)
    if project_font.exists() and project_font.stat().st_size > 0:
        return project_font

    # 2) кэшированный / скачанный шрифт
    cached_font = ensure_cached_font(family, style)
    if cached_font and cached_font.exists():
        return cached_font

    return None


def deployed_font_candidates(font_name: str, is_bold: bool, is_italic: bool):
    family = classify_font_family(font_name)
    style = font_style_key(is_bold, is_italic)

    ordered_styles = [style]
    for extra in ("regular", "italic", "bold", "bolditalic"):
        if extra not in ordered_styles:
            ordered_styles.append(extra)

    candidates = []
    for st in ordered_styles:
        path = resolve_deployed_font(family, st)
        if path is not None:
            candidates.append(path)

    # если для нужного family нет ничего — попробуем serif как универсальный fallback
    if family != "serif":
        for st in ordered_styles:
            path = resolve_deployed_font("serif", st)
            if path is not None and path not in candidates:
                candidates.append(path)

    return candidates


# ---- Работа со шрифтами PyMuPDF ----

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


def font_object_supports_text(font_obj, text):
    for ch in text or "":
        if ch.isspace():
            continue
        try:
            if not font_obj.has_glyph(ord(ch)):
                return False
        except Exception:
            return False
    return True


def font_supports_text(font_path, text):
    font_obj = create_font_object_from_file(font_path)
    if font_obj is None:
        return False
    return font_object_supports_text(font_obj, text)


def text_fits_single_line(font_obj, text, fontsize, width_limit):
    if font_obj is None:
        return False
    try:
        text_width = font_obj.text_length(text, fontsize=fontsize)
        return text_width <= max(width_limit - 1.5, 1)
    except Exception:
        return False


# ---- Извлечение шрифтов для фронта ----

def extract_page_fonts(doc, page):
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
            safe_name = slugify_font_name(base_font)

            if not font_buffer or ext not in BROWSER_SUPPORTED_FONT_EXTENSIONS:
                font_map[base_font.lower()] = {
                    "webFontFamily": None,
                    "webFontUrl": None,
                    "fontStyle": "normal",
                    "fontWeight": "400",
                    "extracted": False,
                }
                continue

            filename = f"{safe_name}.{ext}"
            out_path = EXTRACTED_FONTS_FOLDER / filename
            if not out_path.exists():
                with open(out_path, "wb") as f:
                    f.write(font_buffer)

            is_bold, is_italic = font_traits(base_font)

            font_map[base_font.lower()] = {
                "webFontFamily": safe_name,
                "webFontUrl": f"/extracted_fonts/{filename}",
                "fontStyle": "italic" if is_italic else "normal",
                "fontWeight": "700" if is_bold else "400",
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
        "fontStyle": "normal",
        "fontWeight": "400",
        "extracted": False,
    }


# ---- Разбиение текста на editable units ----

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


def build_unit_from_spans(page_index, spans, page_font_map):
    if not spans:
        return None

    full_text = normalize_pdf_unicode_text("".join(span.get("text", "") for span in spans))
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
    is_bold, is_italic = font_traits(font_name, flags)
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
        "webFontStyle": web_font_info["fontStyle"],
        "webFontWeight": web_font_info["fontWeight"],
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
                key = (
                    unit["webFontFamily"],
                    unit["webFontUrl"],
                    unit["webFontStyle"],
                    unit["webFontWeight"],
                )
                if key not in seen_font_families:
                    seen_font_families.add(key)
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


# ---- Сохранение обратно в PDF ----

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

                text_weight = sum(ch.isalnum() for ch in span.get("text", ""))
                score = area * max(text_weight, 1)

                if score > best_score:
                    best_score = score
                    best_span = span

    return best_span


def find_font_data(doc, page, span_font_name):
    wanted = clean_font_name(span_font_name).lower()

    for font in page.get_fonts(full=True):
        xref = font[0]
        base_font = clean_font_name(font[3] if len(font) > 3 else "").lower()

        if base_font == wanted or wanted in base_font or base_font in wanted:
            try:
                extracted = doc.extract_font(xref)
                font_buffer = extracted[3]
                if font_buffer:
                    return font_buffer
            except Exception:
                pass

    return None


def select_font_for_replacement(doc, page, span, new_text):
    """
    Порядок:
    1. оригинальный embedded font PDF, если он умеет новый текст
    2. проектный / скачанный fallback по семейству+стилю
    """
    original_font_name = span.get("font", "")
    flags = int(span.get("flags", 0))
    is_bold, is_italic = font_traits(original_font_name, flags)

    candidates = []

    # 1) оригинальный embedded font
    original_buffer = find_font_data(doc, page, original_font_name)
    if original_buffer is not None:
        encoded_text = encode_text_for_original_font(original_font_name, new_text)
        font_obj = create_font_object_from_buffer(original_buffer)
        if font_obj is not None and font_object_supports_text(font_obj, encoded_text):
            candidates.append({
                "kind": "buffer",
                "font_obj": font_obj,
                "font_buffer": original_buffer,
                "text": encoded_text,
            })

    # 2) fallback из приложения / кэша
    for font_path in deployed_font_candidates(original_font_name, is_bold, is_italic):
        font_obj = create_font_object_from_file(font_path)
        if font_obj is not None and font_object_supports_text(font_obj, new_text):
            candidates.append({
                "kind": "file",
                "font_obj": font_obj,
                "font_file": str(font_path),
                "text": new_text,
            })

    return candidates


def replace_item_keep_style(
    doc,
    page,
    rect,
    new_text,
    max_x=None,
    background=(1, 1, 1),
):
    span = find_span_for_rect(page, rect)
    if span is None:
        return False

    font_size = float(span.get("size", 12))
    color = int_rgb_to_tuple(span.get("color", 0))

    redraw_rect = fitz.Rect(rect)
    redraw_rect.y0 -= font_size * 0.20
    redraw_rect.y1 += font_size * 0.20

    text_right_limit = float(max_x) if max_x is not None else redraw_rect.x1 + font_size * 2.0
    text_right_limit = min(text_right_limit, page.rect.x1 - 2)
    available_width = max(text_right_limit - redraw_rect.x0, 1)

    baseline_y = span.get("origin", [redraw_rect.x0, redraw_rect.y1])[1]

    candidates = select_font_for_replacement(doc, page, span, new_text)
    if not candidates:
        return False

    chosen = None
    chosen_size = font_size

    for candidate in candidates:
        test_size = font_size
        for _ in range(12):
            if text_fits_single_line(candidate["font_obj"], candidate["text"], test_size, available_width):
                chosen = candidate
                chosen_size = test_size
                break

            test_size -= 0.5
            if test_size < 5:
                break

        if chosen is not None:
            break

    if chosen is None:
        return False

    # Только теперь удаляем старый текст
    try:
        page.add_redact_annot(redraw_rect, fill=background)
        page.apply_redactions()
    except Exception:
        page.draw_rect(redraw_rect, color=background, fill=background, overlay=True)

    try:
        if chosen["kind"] == "buffer":
            font_name = f"ORIG_{uuid.uuid4().hex[:8]}"
            page.insert_font(fontname=font_name, fontbuffer=chosen["font_buffer"])
        else:
            font_name = f"FALLBACK_{uuid.uuid4().hex[:8]}"
            page.insert_font(fontname=font_name, fontfile=chosen["font_file"])

        page.insert_text(
            fitz.Point(redraw_rect.x0, baseline_y),
            chosen["text"],
            fontname=font_name,
            fontsize=chosen_size,
            color=color,
            overlay=True,
        )
        return True
    except Exception:
        return False


# ---- Flask routes ----

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
        traceback.print_exc()
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

    try:
        doc = fitz.open(input_path)

        for change in changes:
            new_text = normalize_pdf_unicode_text(change.get("newText", ""))
            old_text = normalize_pdf_unicode_text(change.get("oldText", ""))

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