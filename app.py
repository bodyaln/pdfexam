from flask import Flask, render_template, request, jsonify, send_from_directory, Response
from pathlib import Path
import fitz  # PyMuPDF
import uuid
import re
import traceback
import unicodedata
import shutil
import subprocess
import tempfile
import hashlib

app = Flask(__name__)

BASE_DIR = Path(__file__).resolve().parent
UPLOAD_FOLDER = BASE_DIR / "uploads"
OUTPUT_FOLDER = BASE_DIR / "outputs"

UPLOAD_FOLDER.mkdir(exist_ok=True)
OUTPUT_FOLDER.mkdir(exist_ok=True)

BROWSER_SUPPORTED_FONT_EXTENSIONS = {"ttf", "otf", "woff", "woff2"}
FONTFORGE_INPUT_EXTENSIONS = {"pfa", "pfb", "cff", "cid", "ttf", "otf"}
RUNTIME_FONTS = {}
BUILTIN_SERIF_FONTNAMES = {
    "regular": "tiro",
    "bold": "tibo",
    "italic": "tiit",
    "bolditalic": "tibi",
}
FONT_COMPOSITE_SCRIPT = r"""
import fontforge
import sys

font = fontforge.open(sys.argv[1])

ACCENTED = {
    0x00E1: ("a", "acute"),
    0x00C1: ("A", "acute"),
    0x00E9: ("e", "acute"),
    0x00C9: ("E", "acute"),
    0x00ED: ("i", "acute"),
    0x00CD: ("I", "acute"),
    0x00F3: ("o", "acute"),
    0x00D3: ("O", "acute"),
    0x00FA: ("u", "acute"),
    0x00DA: ("U", "acute"),
    0x00FD: ("y", "acute"),
    0x00DD: ("Y", "acute"),
    0x013A: ("l", "acute"),
    0x0139: ("L", "acute"),
    0x0155: ("r", "acute"),
    0x0154: ("R", "acute"),
    0x010D: ("c", "caron"),
    0x010C: ("C", "caron"),
    0x0148: ("n", "caron"),
    0x0147: ("N", "caron"),
    0x0161: ("s", "caron"),
    0x0160: ("S", "caron"),
    0x017E: ("z", "caron"),
    0x017D: ("Z", "caron"),
    0x00F4: ("o", "circumflex"),
    0x00D4: ("O", "circumflex"),
    0x00E4: ("a", "dieresis"),
    0x00C4: ("A", "dieresis"),
}

CARON_APOSTROPHE = {
    0x010F: "d",
    0x010E: "D",
    0x013E: "l",
    0x013D: "L",
    0x0165: "t",
    0x0164: "T",
}


def add_centered_accent(code, base_name, accent_name):
    if base_name not in font or accent_name not in font:
        return

    base = font[base_name]
    accent = font[accent_name]
    glyph = font.createChar(code)
    glyph.clear()
    glyph.addReference(base_name)
    dx = int(round((base.width - accent.width) / 2))
    glyph.addReference(accent_name, (1, 0, 0, 1, dx, 0))
    glyph.width = base.width


def add_apostrophe_caron(code, base_name):
    if base_name not in font or "quoteright" not in font:
        return

    base = font[base_name]
    accent = font["quoteright"]
    glyph = font.createChar(code)
    glyph.clear()
    glyph.addReference(base_name)
    dx = int(round(base.width - accent.width * 0.45))
    glyph.addReference("quoteright", (1, 0, 0, 1, dx, 0))
    glyph.width = base.width


for code, names in ACCENTED.items():
    add_centered_accent(code, *names)

for code, base_name in CARON_APOSTROPHE.items():
    add_apostrophe_caron(code, base_name)

font.generate(sys.argv[2])
"""

PREFIX_ACCENT_MARKS = {
    "\u00b4": "\u0301",
    "\u02c7": "\u030c",
    "\u02c6": "\u0302",
    "`": "\u0300",
    "\u00a8": "\u0308",
}
PREFIX_ACCENT_RE = re.compile(
    f"([{re.escape(''.join(PREFIX_ACCENT_MARKS))}])([A-Za-z\u0131])"
)
POSTFIX_CARON_RE = re.compile(r"([dltDLT])[\u2019']")
DIACRITIC_SPAN_CHARS = set(PREFIX_ACCENT_MARKS) | {"\u2019", "'"}

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


def has_non_ascii_diacritics(text: str) -> bool:
    for ch in text or "":
        if ord(ch) > 127 and not ch.isspace():
            return True
    return False


def is_diacritic_span(span) -> bool:
    stripped = (span.get("text", "") or "").strip()
    return bool(stripped) and all(ch in DIACRITIC_SPAN_CHARS for ch in stripped)


def is_neutral_style_span(span) -> bool:
    text = span.get("text", "") or ""
    return not text.strip() or is_diacritic_span(span)


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
        or name.startswith("ec")
        or "computer modern" in name
        or "latinmodern" in name
        or "cmbx" in name
        or "cmr" in name
        or "cmti" in name
    )


def encode_text_for_original_font(font_name, text):
    # Больше НЕ раскладываем словацкие буквы на accent+base.
    # Всегда работаем с нормальным Unicode.
    return normalize_pdf_unicode_text(text)


def classify_font_family(font_name):
    name = clean_font_name(font_name).lower()

    if is_tex_like_font(name):
        return "tex"

    if (
        "times" in name
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
    if family == "tex":
        return "serif"
    return "Times New Roman, Times, serif"


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
    digest = hashlib.sha256(font_buffer).hexdigest()[:24]
    safe_name = slugify_font_name(font_name)
    ext = (ext or "otf").lower()
    font_id = f"{safe_name}_{digest}.{ext}"

    RUNTIME_FONTS[font_id] = {
        "buffer": font_buffer,
        "mimetype": font_mimetype(ext),
    }

    return f"/runtime_fonts/{font_id}"


def font_buffer_supports_text(font_buffer, text) -> bool:
    font_obj = create_font_object_from_buffer(font_buffer)
    if font_obj is None:
        return False

    return font_object_supports_text(font_obj, text)


def create_font_object_from_buffer(font_buffer):
    try:
        return fitz.Font(fontbuffer=font_buffer)
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


def text_fits_single_line(font_obj, text, fontsize, width_limit):
    if font_obj is None:
        return False
    try:
        text_width = font_obj.text_length(text, fontsize=fontsize)
        return text_width <= max(width_limit - 1.5, 1)
    except Exception:
        return False


def make_single_run_candidate(kind, font_obj, text, **source):
    return {
        "runs": [{
            "kind": kind,
            "font_obj": font_obj,
            "text": text,
            **source,
        }],
    }


def candidate_text_width(candidate, fontsize):
    try:
        return sum(
            run["font_obj"].text_length(run["text"], fontsize=fontsize)
            for run in candidate["runs"]
        )
    except Exception:
        return float("inf")


def run_source_key(run):
    if run["kind"] == "buffer":
        return ("buffer", hashlib.sha256(run["font_buffer"]).hexdigest())
    return ("builtin", run["font_name"])


def make_space_safe_buffer_candidate(primary_font_obj, primary_font_buffer, text, space_font_obj, space_font_name):
    runs = []

    for ch in text:
        if ch.isspace():
            run = {
                "kind": "builtin",
                "font_obj": space_font_obj,
                "font_name": space_font_name,
                "text": ch,
            }
        else:
            run = {
                "kind": "buffer",
                "font_obj": primary_font_obj,
                "font_buffer": primary_font_buffer,
                "text": ch,
            }

        if runs and run_source_key(runs[-1]) == run_source_key(run):
            runs[-1]["text"] += ch
        else:
            runs.append(run)

    return {"runs": runs}


def builtin_font_source(is_bold, is_italic):
    builtin_name = BUILTIN_SERIF_FONTNAMES[font_style_key(is_bold, is_italic)]
    try:
        font_obj = fitz.Font(fontname=builtin_name)
    except Exception:
        return None

    return {
        "kind": "builtin",
        "font_obj": font_obj,
        "font_name": builtin_name,
    }


def convert_font_to_unicode_otf(font_buffer: bytes, ext: str) -> bytes | None:
    ext = (ext or "").lower()
    if ext not in FONTFORGE_INPUT_EXTENSIONS:
        return None

    fontforge_bin = shutil.which("fontforge")
    if fontforge_bin is None:
        return None

    try:
        with tempfile.TemporaryDirectory(prefix="pdfedit-font-") as tmpdir:
            source_path = Path(tmpdir) / f"source.{ext}"
            output_path = Path(tmpdir) / "font_unicode.otf"
            source_path.write_bytes(font_buffer)

            subprocess.run(
                [
                    fontforge_bin,
                    "-lang=py",
                    "-c",
                    FONT_COMPOSITE_SCRIPT,
                    str(source_path),
                    str(output_path),
                ],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=30,
            )

            if output_path.exists() and output_path.stat().st_size > 0:
                return output_path.read_bytes()
    except Exception:
        return None

    return None


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

            if not font_buffer or not ext:
                font_map[base_font.lower()] = {
                    "webFontFamily": None,
                    "webFontUrl": None,
                    "webFontBuffer": None,
                    "augmentedFontBuffer": None,
                    "fontStyle": "normal",
                    "fontWeight": "400",
                    "extracted": False,
                }
                continue

            final_url = None
            final_buffer = None
            final_ext = None
            if ext in BROWSER_SUPPORTED_FONT_EXTENSIONS:
                final_buffer = font_buffer
                final_ext = ext
            else:
                converted_buffer = convert_font_to_unicode_otf(font_buffer, ext)
                if converted_buffer is not None:
                    final_buffer = converted_buffer
                    final_ext = "otf"

            augmented_buffer = None
            if ext in FONTFORGE_INPUT_EXTENSIONS:
                augmented_buffer = convert_font_to_unicode_otf(font_buffer, ext)

            if final_buffer is not None:
                final_url = register_runtime_font(final_buffer, final_ext, safe_name)

            is_bold, is_italic = font_traits(base_font)

            font_map[base_font.lower()] = {
                "webFontFamily": safe_name,
                "webFontUrl": final_url,
                "webFontBuffer": final_buffer,
                "augmentedFontBuffer": augmented_buffer,
                "fontStyle": "italic" if is_italic else "normal",
                "fontWeight": "700" if is_bold else "400",
                "extracted": final_url is not None,
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
        "webFontBuffer": None,
        "augmentedFontBuffer": None,
        "fontStyle": "normal",
        "fontWeight": "400",
        "extracted": False,
    }


def spans_have_same_style(a, b):
    if clean_font_name(a.get("font", "")).lower() != clean_font_name(b.get("font", "")).lower():
        return False

    if abs(float(a.get("size", 0)) - float(b.get("size", 0))) > 0.25:
        return False

    if (
        int(a.get("flags", 0)) != int(b.get("flags", 0))
        and not is_neutral_style_span(a)
        and not is_neutral_style_span(b)
    ):
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

    augmented_buffer = web_font_info.get("augmentedFontBuffer")
    if (
        augmented_buffer
        and not font_buffer_supports_text(web_font_info.get("webFontBuffer"), full_text)
        and font_buffer_supports_text(augmented_buffer, full_text)
    ):
        web_font_info = {
            **web_font_info,
            "webFontUrl": register_runtime_font(augmented_buffer, "otf", font_name),
            "webFontBuffer": augmented_buffer,
            "extracted": True,
        }

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


def extract_original_font_resource(doc, page, span_font_name):
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
                        "font_buffer": font_buffer,
                        "font_ext": ext,
                        "base_font": base_font,
                    }
            except Exception:
                pass

    return None


def select_font_for_replacement(doc, page, span, new_text):
    """
    Prefer the original embedded font from the uploaded PDF. If it is a Type1
    font or misses precomposed Slovak glyphs, build an in-memory Unicode OTF
    from the embedded font via FontForge and use that for insertion.
    """
    original_font_name = span.get("font", "")
    flags = int(span.get("flags", 0))
    is_bold, is_italic = font_traits(original_font_name, flags)

    candidates = []
    normalized_text = normalize_pdf_unicode_text(new_text)

    fallback_source = builtin_font_source(is_bold, is_italic)

    font_resource = extract_original_font_resource(doc, page, original_font_name)

    if font_resource is not None:
        original_buffer = font_resource["font_buffer"]
        original_font_obj = create_font_object_from_buffer(original_buffer)

        if (
            original_font_obj is not None
            and not any(ch.isspace() for ch in normalized_text)
            and font_object_supports_text(original_font_obj, normalized_text)
        ):
            candidates.append(make_single_run_candidate(
                "buffer",
                original_font_obj,
                normalized_text,
                font_buffer=original_buffer,
            ))

        converted_buffer = convert_font_to_unicode_otf(
            original_buffer,
            font_resource["font_ext"],
        )
        converted_font_obj = create_font_object_from_buffer(converted_buffer)
        if (
            converted_buffer is not None
            and converted_font_obj is not None
            and font_object_supports_text(converted_font_obj, normalized_text)
        ):
            if fallback_source is not None and any(ch.isspace() for ch in normalized_text):
                candidates.append(make_space_safe_buffer_candidate(
                    converted_font_obj,
                    converted_buffer,
                    normalized_text,
                    fallback_source["font_obj"],
                    fallback_source["font_name"],
                ))
            else:
                candidates.append(make_single_run_candidate(
                    "buffer",
                    converted_font_obj,
                    normalized_text,
                    font_buffer=converted_buffer,
                ))

    if fallback_source is not None and font_object_supports_text(
        fallback_source["font_obj"],
        normalized_text,
    ):
        candidates.append(make_single_run_candidate(
            "builtin",
            fallback_source["font_obj"],
            normalized_text,
            font_name=fallback_source["font_name"],
        ))

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
            if candidate_text_width(candidate, test_size) <= max(available_width - 1.5, 1):
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

    try:
        page.add_redact_annot(redraw_rect, fill=background)
        page.apply_redactions()
    except Exception:
        page.draw_rect(redraw_rect, color=background, fill=background, overlay=True)

    try:
        cursor_x = redraw_rect.x0
        inserted_fonts = {}

        for run in chosen["runs"]:
            key = run_source_key(run)

            if key in inserted_fonts:
                font_name = inserted_fonts[key]
            elif run["kind"] == "buffer":
                font_name = f"ORIG_{uuid.uuid4().hex[:8]}"
                page.insert_font(fontname=font_name, fontbuffer=run["font_buffer"])
                inserted_fonts[key] = font_name
            else:
                font_name = run["font_name"]
                inserted_fonts[key] = font_name

            page.insert_text(
                fitz.Point(cursor_x, baseline_y),
                run["text"],
                fontname=font_name,
                fontsize=chosen_size,
                color=color,
                overlay=True,
            )
            cursor_x += run["font_obj"].text_length(run["text"], fontsize=chosen_size)

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


@app.route("/runtime_fonts/<path:font_id>")
def runtime_font_file(font_id):
    item = RUNTIME_FONTS.get(font_id)
    if item is None:
        return jsonify({"error": "Шрифт не найден в runtime cache"}), 404

    return Response(
        item["buffer"],
        mimetype=item["mimetype"],
        headers={"Cache-Control": "no-store"},
    )


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5001, debug=True, use_reloader=False)
