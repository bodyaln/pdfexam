let currentFilename = null;
let currentPages = [];
let currentPdfUrl = null;
const scale = 1.5;

pdfjsLib.GlobalWorkerOptions.workerSrc =
    "https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.worker.min.js";

const pdfFileInput = document.getElementById("pdfFile");
const openBtn = document.getElementById("openBtn");
const applyBtn = document.getElementById("applyBtn");
const viewer = document.getElementById("viewer");
const statusEl = document.getElementById("status");

const COVER_PAD_X = 2;
const COVER_PAD_Y = 1;
const TEXT_WIDTH_PAD = 4;
const MIN_TEXT_WIDTH = 8;
const MIN_LETTER_SPACING = -1.2;
const MAX_LETTER_SPACING = 1.8;

openBtn.addEventListener("click", openPdf);
applyBtn.addEventListener("click", applyPdfChanges);

function setStatus(text, isError = false) {
    statusEl.textContent = text;
    statusEl.classList.toggle("status-error", isError);
}

function normalize(text) {
    return (text || "")
        .replace(/\u00a0/g, " ")
        .replace(/\r/g, "")
        .trim();
}

function editorText(el) {
    return (el?.textContent || "").replace(/\r/g, "");
}

async function registerFontFaces(fontFaces) {
    let styleTag = document.getElementById("dynamic-font-faces");
    if (!styleTag) {
        styleTag = document.createElement("style");
        styleTag.id = "dynamic-font-faces";
        document.head.appendChild(styleTag);
    }

    styleTag.textContent = (fontFaces || [])
        .map(
            (font) => `
@font-face {
  font-family: "${font.fontFamily}";
  src: url("${font.url}");
  font-style: ${font.fontStyle || "normal"};
  font-weight: ${font.fontWeight || "400"};
}
`,
        )
        .join("\n");

    if (document.fonts) {
        try {
            await Promise.all(
                (fontFaces || []).map((font) =>
                    document.fonts.load(
                        `${font.fontStyle || "normal"} ${font.fontWeight || "400"} 16px "${font.fontFamily}"`,
                    ),
                ),
            );
            await document.fonts.ready;
        } catch (e) {
            console.warn("document.fonts.load failed", e);
        }
    }
}

function placeCursorAtEnd(el) {
    const range = document.createRange();
    const sel = window.getSelection();
    range.selectNodeContents(el);
    range.collapse(false);
    sel.removeAllRanges();
    sel.addRange(range);
}

function getSelectionOffsets(element) {
    const selection = window.getSelection();
    if (!selection || selection.rangeCount === 0) {
        const len = editorText(element).length;
        return { start: len, end: len };
    }

    const range = selection.getRangeAt(0);

    if (
        !element.contains(range.startContainer) ||
        !element.contains(range.endContainer)
    ) {
        const len = editorText(element).length;
        return { start: len, end: len };
    }

    const startRange = range.cloneRange();
    startRange.selectNodeContents(element);
    startRange.setEnd(range.startContainer, range.startOffset);
    const start = startRange.toString().length;

    const endRange = range.cloneRange();
    endRange.selectNodeContents(element);
    endRange.setEnd(range.endContainer, range.endOffset);
    const end = endRange.toString().length;

    return { start, end };
}

function buildPredictedText(currentText, insertText, start, end) {
    return currentText.slice(0, start) + insertText + currentText.slice(end);
}

function clamp(value, min, max) {
    return Math.min(Math.max(value, min), max);
}

function getTextMeasurer() {
    let measurer = document.getElementById("text-width-measurer");
    if (!measurer) {
        measurer = document.createElement("span");
        measurer.id = "text-width-measurer";
        measurer.style.position = "absolute";
        measurer.style.left = "-99999px";
        measurer.style.top = "-99999px";
        measurer.style.visibility = "hidden";
        measurer.style.whiteSpace = "nowrap";
        document.body.appendChild(measurer);
    }

    return measurer;
}

function measureRawTextWidthForElement(el, text) {
    const measurer = getTextMeasurer();
    measurer.style.fontFamily = el.style.fontFamily;
    measurer.style.fontSize = el.style.fontSize;
    measurer.style.fontStyle = el.style.fontStyle;
    measurer.style.fontWeight = el.style.fontWeight;
    measurer.style.lineHeight = el.style.lineHeight || "1.2";
    measurer.style.letterSpacing = "0px";
    measurer.textContent = text || "";
    return measurer.getBoundingClientRect().width;
}

function getCharCount(text) {
    return Array.from(text || "").length;
}

function calculatePdfLetterSpacing(el, text) {
    const targetWidth = Number(
        el.dataset.targetTextWidth ||
            el.dataset.originalWidth ||
            MIN_TEXT_WIDTH,
    );
    const rawWidth = measureRawTextWidthForElement(el, text);
    const chars = getCharCount(text);

    if (chars < 2 || rawWidth <= 0 || targetWidth <= 0) {
        return 0;
    }

    const ratio = rawWidth / targetWidth;
    if (ratio < 0.72 || ratio > 1.35) {
        return 0;
    }

    return clamp(
        (targetWidth - rawWidth) / (chars - 1),
        MIN_LETTER_SPACING,
        MAX_LETTER_SPACING,
    );
}

function applyPdfLetterSpacing(el) {
    const changed = editorHasChanges(el);
    const editing = document.activeElement === el;

    if (changed || editing) {
        el.style.letterSpacing = "0px";
        return 0;
    }

    const spacing = calculatePdfLetterSpacing(el, editorText(el));
    el.style.letterSpacing = `${spacing}px`;
    return spacing;
}

function measureTextWidthForElement(el, text) {
    const rawWidth = measureRawTextWidthForElement(el, text);
    const spacing = Number.parseFloat(el.style.letterSpacing || "0") || 0;
    const chars = getCharCount(text);
    return (
        Math.ceil(rawWidth + spacing * Math.max(chars - 1, 0)) + TEXT_WIDTH_PAD
    );
}

function getMaxAllowedWidth(el) {
    const wrapper = el.parentElement;
    if (!wrapper) return MIN_TEXT_WIDTH;
    return Math.max(wrapper.clientWidth - 2, MIN_TEXT_WIDTH);
}

function fitsTextInBlock(el, candidateText) {
    const prevSpacing = el.style.letterSpacing;
    el.style.letterSpacing = "0px";
    const fits =
        Math.ceil(measureRawTextWidthForElement(el, candidateText)) +
            TEXT_WIDTH_PAD <=
        getMaxAllowedWidth(el);
    el.style.letterSpacing = prevSpacing;
    return fits;
}

function resizeBlockToText(el) {
    applyPdfLetterSpacing(el);

    const text = editorText(el);
    const measured = measureTextWidthForElement(el, text);
    const maxAllowed = getMaxAllowedWidth(el);
    const finalWidth = Math.min(Math.max(measured, MIN_TEXT_WIDTH), maxAllowed);
    el.style.width = `${finalWidth}px`;
    return measured <= maxAllowed;
}

function editorHasChanges(editor) {
    return normalize(editorText(editor)) !== normalize(editor.dataset.oldText);
}

function setEditorVisibility(editor, visible) {
    const wrapper = editor.parentElement;
    const cover = wrapper?.querySelector(".overlay-cover");

    if (visible) {
        editor.classList.remove("overlay-hidden");
        if (cover) cover.style.display = "block";
    } else {
        editor.classList.add("overlay-hidden");
        if (cover) cover.style.display = "none";
    }
}

function updateCoverGeometry(editor) {
    const wrapper = editor.parentElement;
    const cover = wrapper?.querySelector(".overlay-cover");
    if (!cover) return;

    const targetWidth = Number(
        editor.dataset.targetTextWidth ||
            editor.dataset.originalWidth ||
            MIN_TEXT_WIDTH,
    );
    const maxWidth = Number(editor.dataset.maxWidth || targetWidth);
    const textWidth = measureTextWidthForElement(editor, editorText(editor));
    const coverLeft = -COVER_PAD_X;
    const coverRight = Math.min(
        Math.max(targetWidth, textWidth) + COVER_PAD_X,
        maxWidth,
    );
    const wrapperHeight =
        Number.parseFloat(wrapper.style.height || "0") || wrapper.clientHeight;

    cover.style.left = `${coverLeft}px`;
    cover.style.top = `${COVER_PAD_Y}px`;
    cover.style.width = `${Math.max(coverRight - coverLeft, MIN_TEXT_WIDTH)}px`;
    cover.style.height = `${Math.max(wrapperHeight - COVER_PAD_Y * 2, 1)}px`;
}

function updateEditorVisualState(editor) {
    const wrapper = editor.parentElement;
    const changed = editorHasChanges(editor);
    const editing = document.activeElement === editor;

    editor.classList.toggle("changed", changed);

    if (wrapper) {
        wrapper.classList.toggle("changed", changed);
        wrapper.classList.toggle("editing", editing);
    }

    if (editing || changed) {
        setEditorVisibility(editor, true);
    } else {
        setEditorVisibility(editor, false);
    }

    resizeBlockToText(editor);
    updateCoverGeometry(editor);
}

function createOverlayDiv(unit, pageData) {
    const wrapper = document.createElement("div");
    wrapper.className = "overlay-wrapper";

    const cover = document.createElement("div");
    cover.className = "overlay-cover";

    const editor = document.createElement("div");
    editor.className = "overlay-text overlay-hidden";
    editor.contentEditable = "true";
    editor.spellcheck = false;
    editor.textContent = unit.text;

    const left = unit.x0 * scale;

    // Позиционирование от baseline, чтобы курсив в скобках совпадал лучше
    const top = (unit.originY - unit.size * 0.92) * scale;

    const targetWidth = Math.max((unit.x1 - unit.x0) * scale, MIN_TEXT_WIDTH);
    const baseWidth = Math.max(targetWidth + TEXT_WIDTH_PAD, 20);
    const baseHeight = Math.max(unit.size * scale * 1.35, 20);
    const maxX = (unit.maxX ?? pageData.width) * scale;
    const maxWidth = Math.max(maxX - left, baseWidth);

    wrapper.style.position = "absolute";
    wrapper.style.left = `${left}px`;
    wrapper.style.top = `${top}px`;
    wrapper.style.width = `${maxWidth}px`;
    wrapper.style.height = `${baseHeight}px`;
    wrapper.style.overflow = "visible";

    cover.style.position = "absolute";
    cover.style.left = `0px`;
    cover.style.top = `${COVER_PAD_Y}px`;
    cover.style.width = `${baseWidth}px`;
    cover.style.height = `${Math.max(baseHeight - COVER_PAD_Y * 2, 1)}px`;
    cover.style.background = "#ffffff";
    cover.style.display = "none";

    editor.style.position = "absolute";
    editor.style.left = "0px";
    editor.style.top = "0px";
    editor.style.width = `${baseWidth}px`;
    editor.style.minWidth = "8px";
    editor.style.height = `${baseHeight}px`;
    editor.style.fontSize = `${unit.size * scale}px`;
    editor.style.whiteSpace = "pre";

    if (unit.webFontFamily) {
        editor.style.fontFamily = `"${unit.webFontFamily}", ${unit.browserFont || "serif"}`;
    } else {
        editor.style.fontFamily =
            unit.browserFont || "Times New Roman, Times, serif";
    }

    editor.style.fontStyle = unit.isItalic ? "italic" : "normal";
    editor.style.fontWeight = unit.isBold ? "bold" : "normal";

    editor.dataset.page = unit.page;
    editor.dataset.x0 = unit.x0;
    editor.dataset.y0 = unit.y0;
    editor.dataset.x1 = unit.x1;
    editor.dataset.y1 = unit.y1;
    editor.dataset.maxX = unit.maxX ?? pageData.width;

    editor.dataset.font = unit.font || "";
    editor.dataset.size = unit.size;
    editor.dataset.color = unit.color;
    editor.dataset.flags = unit.flags;
    editor.dataset.oldText = unit.originalText || unit.text;
    editor.dataset.prevText = unit.text;
    editor.dataset.isItalic = unit.isItalic ? "1" : "0";
    editor.dataset.isBold = unit.isBold ? "1" : "0";
    editor.dataset.originalWidth = baseWidth;
    editor.dataset.targetTextWidth = targetWidth;
    editor.dataset.maxWidth = maxWidth;
    editor.dataset.originY = unit.originY;

    editor.addEventListener("focus", () => {
        wrapper.classList.add("editing");
        setEditorVisibility(editor, true);
        resizeBlockToText(editor);
        updateEditorVisualState(editor);
    });

    editor.addEventListener("blur", () => {
        wrapper.classList.remove("editing");
        if (!editorHasChanges(editor)) {
            editor.style.width = `${editor.dataset.originalWidth}px`;
        }
        updateEditorVisualState(editor);
    });

    editor.addEventListener("input", () => {
        if (!fitsTextInBlock(editor, editorText(editor))) {
            editor.textContent =
                editor.dataset.prevText || editor.dataset.oldText || "";
            placeCursorAtEnd(editor);
        }

        editor.dataset.prevText = editorText(editor);
        updateEditorVisualState(editor);
    });

    editor.addEventListener("beforeinput", (e) => {
        const inputType = e.inputType || "";

        if (
            inputType === "deleteContentBackward" ||
            inputType === "deleteContentForward" ||
            inputType === "deleteByCut" ||
            inputType === "deleteByDrag"
        ) {
            return;
        }

        if (
            inputType === "insertParagraph" ||
            inputType === "insertLineBreak"
        ) {
            e.preventDefault();
            return;
        }

        if (
            inputType === "insertText" ||
            inputType === "insertFromPaste" ||
            inputType === "insertCompositionText"
        ) {
            const currentText = editorText(editor);
            const { start, end } = getSelectionOffsets(editor);

            let incomingText = e.data ?? "";

            if (inputType === "insertFromPaste") {
                incomingText = (
                    (e.clipboardData || window.clipboardData)?.getData(
                        "text",
                    ) || ""
                ).replace(/\r?\n/g, " ");
            }

            incomingText = (incomingText || "").replace(/\r?\n/g, " ");

            const predictedText = buildPredictedText(
                currentText,
                incomingText,
                start,
                end,
            );

            if (!fitsTextInBlock(editor, predictedText)) {
                e.preventDefault();
                placeCursorAtEnd(editor);
            }
        }
    });

    wrapper.addEventListener("mousedown", () => {
        if (document.activeElement !== editor) {
            setEditorVisibility(editor, true);
            requestAnimationFrame(() => {
                editor.focus();
                placeCursorAtEnd(editor);
            });
        }
    });

    wrapper.appendChild(cover);
    wrapper.appendChild(editor);
    updateEditorVisualState(editor);
    return wrapper;
}

async function openPdf() {
    const file = pdfFileInput.files[0];
    if (!file) {
        alert("Выбери PDF файл");
        return;
    }
    try {
        setStatus("Загрузка PDF...");
        applyBtn.disabled = true;
        viewer.innerHTML = "";

        const formData = new FormData();
        formData.append("pdf", file);

        const response = await fetch("/api/pdf/open", {
            method: "POST",
            body: formData,
        });

        const data = await response.json();

        if (!response.ok || data.error) {
            throw new Error(data.error || "Ошибка открытия PDF");
        }

        currentFilename = data.filename;
        currentPages = data.pages || [];
        currentPdfUrl = data.pdfUrl;

        await registerFontFaces(data.fontFaces || []);
        await renderPdfWithOverlay(currentPdfUrl, currentPages);

        applyBtn.disabled = false;
        setStatus("PDF загружен");
    } catch (error) {
        console.error(error);
        setStatus(error.message || "Ошибка открытия PDF", true);
    }
}

function resizeAllOverlayTexts() {
    const editors = document.querySelectorAll(".overlay-text");
    editors.forEach((editor) => {
        if (!editorHasChanges(editor) && document.activeElement !== editor) {
            editor.style.width = `${editor.dataset.originalWidth}px`;
        }
        updateEditorVisualState(editor);
    });
}

async function renderPdfWithOverlay(pdfUrl, pagesData) {
    viewer.innerHTML = "";
    const pdf = await pdfjsLib.getDocument(pdfUrl).promise;

    for (let i = 0; i < pdf.numPages; i++) {
        const page = await pdf.getPage(i + 1);
        const viewport = page.getViewport({ scale });

        const pageWrapper = document.createElement("div");
        pageWrapper.className = "page-wrapper";
        pageWrapper.style.width = `${viewport.width}px`;
        pageWrapper.style.height = `${viewport.height}px`;

        const canvas = document.createElement("canvas");
        canvas.className = "pdf-canvas";
        canvas.width = Math.floor(viewport.width);
        canvas.height = Math.floor(viewport.height);

        const context = canvas.getContext("2d");
        await page.render({
            canvasContext: context,
            viewport,
        }).promise;

        pageWrapper.appendChild(canvas);

        const overlayLayer = document.createElement("div");
        overlayLayer.className = "overlay-layer";
        overlayLayer.style.width = `${viewport.width}px`;
        overlayLayer.style.height = `${viewport.height}px`;

        const pageData = pagesData[i];
        const units = pageData?.units || [];

        for (const unit of units) {
            overlayLayer.appendChild(createOverlayDiv(unit, pageData));
        }

        pageWrapper.appendChild(overlayLayer);
        viewer.appendChild(pageWrapper);
    }

    resizeAllOverlayTexts();
}

function collectChanges() {
    const editors = Array.from(document.querySelectorAll(".overlay-text"));
    const changes = [];
    for (const el of editors) {
        const oldText = el.dataset.oldText || "";
        const newText = editorText(el);

        if (normalize(oldText) === normalize(newText)) {
            continue;
        }

        changes.push({
            page: Number(el.dataset.page),
            oldText,
            newText,
            x0: Number(el.dataset.x0),
            y0: Number(el.dataset.y0),
            x1: Number(el.dataset.x1),
            y1: Number(el.dataset.y1),
            maxX: Number(el.dataset.maxX),
            font: el.dataset.font || "",
            size: Number(el.dataset.size),
            color: Number(el.dataset.color),
            flags: Number(el.dataset.flags),
            originY: Number(el.dataset.originY),
        });
    }

    return changes;
}

async function applyPdfChanges() {
    if (!currentFilename) {
        alert("Сначала загрузи PDF");
        return;
    }
    try {
        const changes = collectChanges();

        if (changes.length === 0) {
            alert("Нет изменений для сохранения");
            return;
        }

        setStatus("Сохранение PDF...");
        applyBtn.disabled = true;

        const response = await fetch("/api/pdf/apply", {
            method: "POST",
            headers: {
                "Content-Type": "application/json",
            },
            body: JSON.stringify({
                filename: currentFilename,
                changes,
            }),
        });

        const data = await response.json();

        if (!response.ok || data.error) {
            throw new Error(data.error || "Ошибка сохранения PDF");
        }

        setStatus("PDF сохранён");
        window.location.href = data.downloadUrl;
    } catch (error) {
        console.error(error);
        setStatus(error.message || "Ошибка сохранения PDF", true);
    } finally {
        applyBtn.disabled = false;
    }
}
