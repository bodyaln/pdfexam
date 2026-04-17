let currentFilename = null;
let currentPages = [];
let currentPdfUrl = null;
const scale = 1.5;
const appBasePath = (() => {
    const scriptPath = document.currentScript?.src
        ? new URL(document.currentScript.src).pathname
        : "";
    const marker = "/static/app.js";
    return scriptPath.endsWith(marker)
        ? scriptPath.slice(0, -marker.length)
        : "";
})();

function appUrl(path) {
    const cleanPath = String(path || "").replace(/^\/+/, "");
    return appBasePath ? `${appBasePath}/${cleanPath}` : `/${cleanPath}`;
}

pdfjsLib.GlobalWorkerOptions.workerSrc =
    "https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.worker.min.js";

const pdfFileInput = document.getElementById("pdfFile");
const choosePdfBtn = document.getElementById("choosePdfBtn");
const fileNameEl = document.getElementById("fileName");
const applyBtn = document.getElementById("applyBtn");
const viewer = document.getElementById("viewer");
const statusEl = document.getElementById("status");

const COVER_PAD_X = 2;
const COVER_PAD_Y = 1;
const TEXT_WIDTH_PAD = 4;
const MIN_TEXT_WIDTH = 8;
const MIN_LETTER_SPACING = -1.2;
const MAX_LETTER_SPACING = 1.8;
const MIN_BLOCK_GAP = 2;
const MAX_FLOW_GAP = 24;
const HTML_BASELINE_RATIO = 0.78;

choosePdfBtn.addEventListener("click", openPdfPicker);
pdfFileInput.addEventListener("change", openPdf);
applyBtn.addEventListener("click", applyPdfChanges);

// Открывает системное окно выбора PDF-файла.
function openPdfPicker() {
    pdfFileInput.value = "";
    pdfFileInput.click();
}

// Обновляет текст статуса и помечает его как ошибку при необходимости.
function setStatus(text, isError = false) {
    statusEl.textContent = text;
    statusEl.classList.toggle("status-error", isError);
}

// Нормализует текст для сравнения, но сохраняет пробелы как реальные символы.
function normalize(text) {
    return (text || "").replace(/\u00a0/g, " ").replace(/\r/g, "");
}

// Возвращает чистый текст из редактируемого HTML-блока.
function editorText(el) {
    return (el?.textContent || "").replace(/\r/g, "");
}

// Регистрирует шрифты, которые сервер извлек из PDF.
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
  src: url("${appUrl(font.url)}");
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
            console.warn("Načítanie fontov zlyhalo", e);
        }
    }
}

// Ставит курсор в конец редактируемого блока.
function placeCursorAtEnd(el) {
    const range = document.createRange();
    const sel = window.getSelection();
    range.selectNodeContents(el);
    range.collapse(false);
    sel.removeAllRanges();
    sel.addRange(range);
}

// Ставит курсор в указанную позицию внутри редактируемого блока.
function setCursorOffset(el, offset) {
    const selection = window.getSelection();
    const textNode = el.firstChild;
    const safeOffset = Math.max(0, Math.min(offset, editorText(el).length));

    if (!selection) return;

    if (!textNode) {
        placeCursorAtEnd(el);
        return;
    }

    const range = document.createRange();
    range.setStart(textNode, Math.min(safeOffset, textNode.textContent.length));
    range.collapse(true);
    selection.removeAllRanges();
    selection.addRange(range);
}

// Получает позиции начала и конца текущего выделения внутри блока.
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

// Получает текст, выделенный внутри конкретного редактируемого блока.
function getSelectedTextInside(element) {
    const selection = window.getSelection();
    if (!selection || selection.rangeCount === 0) {
        return "";
    }

    const range = selection.getRangeAt(0);
    if (
        !element.contains(range.startContainer) ||
        !element.contains(range.endContainer)
    ) {
        return "";
    }

    return range.toString();
}

// Собирает текст после предполагаемой вставки.
function buildPredictedText(currentText, insertText, start, end) {
    return currentText.slice(0, start) + insertText + currentText.slice(end);
}

// Собирает текст после удаления символа или выделенного фрагмента.
function buildDeletedText(currentText, start, end, direction) {
    if (start !== end) {
        return {
            text: currentText.slice(0, start) + currentText.slice(end),
            cursor: start,
        };
    }

    if (direction === "forward") {
        if (start >= currentText.length) {
            return { text: currentText, cursor: start };
        }

        const nextChars = Array.from(currentText.slice(start));
        const deleteLength = nextChars[0]?.length || 1;

        return {
            text:
                currentText.slice(0, start) +
                currentText.slice(start + deleteLength),
            cursor: start,
        };
    }

    if (start <= 0) {
        return { text: currentText, cursor: start };
    }

    const previousChars = Array.from(currentText.slice(0, start));
    const previousChar = previousChars[previousChars.length - 1] || "";
    const deleteLength = previousChar.length || 1;
    const cursor = Math.max(start - deleteLength, 0);

    return {
        text: currentText.slice(0, cursor) + currentText.slice(start),
        cursor,
    };
}

// Ограничивает число заданным диапазоном.
function clamp(value, min, max) {
    return Math.min(Math.max(value, min), max);
}

// Создает или возвращает скрытый элемент для измерения ширины текста.
function getTextMeasurer() {
    let measurer = document.getElementById("text-width-measurer");
    if (!measurer) {
        measurer = document.createElement("span");
        measurer.id = "text-width-measurer";
        measurer.style.position = "absolute";
        measurer.style.left = "-99999px";
        measurer.style.top = "-99999px";
        measurer.style.visibility = "hidden";
        measurer.style.whiteSpace = "pre";
        document.body.appendChild(measurer);
    }
    return measurer;
}

// Измеряет реальную ширину текста с учетом стиля конкретного блока.
function measureRawTextWidthForElement(el, text) {
    const measurer = getTextMeasurer();
    measurer.style.fontFamily = el.style.fontFamily;
    measurer.style.fontSize = el.style.fontSize;
    measurer.style.fontStyle = el.style.fontStyle;
    measurer.style.fontWeight = el.style.fontWeight;
    measurer.style.lineHeight = el.style.lineHeight || "1.2";
    measurer.style.letterSpacing = "0px";
    measurer.style.whiteSpace = "pre";
    measurer.textContent = text || "";
    return measurer.getBoundingClientRect().width;
}

// Считает количество символов с учетом Unicode-пар.
function getCharCount(text) {
    return Array.from(text || "").length;
}

// Подбирает межбуквенный интервал, чтобы текст ближе совпадал с PDF.
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

function editorHasChanges(editor) {
    return normalize(editorText(editor)) !== normalize(editor.dataset.oldText);
}

// Проверяет, был ли блок сдвинут относительно исходной позиции.
function editorMoved(editor) {
    const originalLeft = Number(editor.dataset.leftPx || 0);
    const dynamicLeft = Number(editor.dataset.dynamicLeftPx || originalLeft);
    return Math.abs(dynamicLeft - originalLeft) > 0.5;
}

// Применяет межбуквенный интервал к неизмененному тексту.
function applyPdfLetterSpacing(el) {
    const changed = editorHasChanges(el);

    if (changed) {
        el.style.letterSpacing = "0px";
        return 0;
    }

    const spacing = calculatePdfLetterSpacing(el, editorText(el));
    el.style.letterSpacing = `${spacing}px`;
    return spacing;
}

// Измеряет ширину текста с учетом текущего межбуквенного интервала.
function measureTextWidthForElement(el, text) {
    const rawWidth = measureRawTextWidthForElement(el, text);
    const spacing = Number.parseFloat(el.style.letterSpacing || "0") || 0;
    const chars = getCharCount(text);
    return (
        Math.ceil(rawWidth + spacing * Math.max(chars - 1, 0)) + TEXT_WIDTH_PAD
    );
}

// Возвращает минимальную ширину, которая нужна блоку для текущего текста.
function getRequiredTextWidth(el) {
    applyPdfLetterSpacing(el);
    return Math.max(
        measureTextWidthForElement(el, editorText(el)),
        MIN_TEXT_WIDTH,
    );
}

// Находит следующий связанный блок на той же строке.
function getNextEditor(editor) {
    const nextId = editor.dataset.nextEditorId;
    if (!nextId) return null;
    return document.querySelector(`.overlay-text[data-editor-id="${nextId}"]`);
}

// Считает доступную ширину от текущей позиции блока до края страницы.
function getPageRemainingWidth(el) {
    const wrapper = el.parentElement;
    if (!wrapper) return MIN_TEXT_WIDTH;

    const currentLeft = Number(
        el.dataset.dynamicLeftPx || el.dataset.leftPx || 0,
    );
    const pageWidthPx = Number(
        el.dataset.pageWidthPx || wrapper.clientWidth || 0,
    );

    return Math.max(pageWidthPx - currentLeft - 2, MIN_TEXT_WIDTH);
}

// Считает максимальную ширину блока с учетом следующего блока или края страницы.
function getMaxAllowedWidth(el) {
    const wrapper = el.parentElement;
    if (!wrapper) return MIN_TEXT_WIDTH;

    const pushedNext = getNextEditor(el);

    if (!pushedNext) {
        return getPageRemainingWidth(el);
    }

    const currentLeft = Number(
        el.dataset.dynamicLeftPx || el.dataset.leftPx || 0,
    );
    const nextLeft = Number(
        pushedNext.dataset.dynamicLeftPx || pushedNext.dataset.leftPx || 0,
    );

    return Math.max(nextLeft - currentLeft, MIN_TEXT_WIDTH);
}

// Проверяет, помещается ли предполагаемый текст в доступную ширину блока.
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

// Подгоняет ширину блока под текст и возвращает, поместился ли текст.
function resizeBlockToText(el, options = {}) {
    applyPdfLetterSpacing(el);

    const text = editorText(el);
    const measured = measureTextWidthForElement(el, text);
    const maxAllowed = options.allowPush
        ? getPageRemainingWidth(el)
        : getMaxAllowedWidth(el);
    const finalWidth = Math.min(Math.max(measured, MIN_TEXT_WIDTH), maxAllowed);
    el.style.width = `${finalWidth}px`;
    return measured <= maxAllowed;
}

// Показывает или скрывает редактируемый HTML-блок и белую подложку.
function setEditorVisibility(editor, visible, coverVisible = false) {
    const wrapper = editor.parentElement;
    const cover = wrapper?.querySelector(".overlay-cover");

    if (visible) {
        editor.classList.remove("overlay-hidden");
        if (cover) cover.style.display = coverVisible ? "block" : "none";
    } else {
        editor.classList.add("overlay-hidden");
        if (cover) cover.style.display = "none";
    }
}

// Пересчитывает размер белой подложки под текущий текст.
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
    const editorLeft = Number.parseFloat(editor.style.left || "0") || 0;
    const textWidth = measureTextWidthForElement(editor, editorText(editor));
    const textRight = editorLeft + textWidth;
    const coverLeft = Math.min(0, editorLeft) - COVER_PAD_X;
    const coverRight = Math.min(
        Math.max(targetWidth, textRight) + COVER_PAD_X,
        maxWidth,
    );
    const wrapperHeight =
        Number.parseFloat(wrapper.style.height || "0") || wrapper.clientHeight;
    const editorHeight =
        Number.parseFloat(editor.style.height || "0") || wrapperHeight;
    const pdfTop = Number(editor.dataset.pdfTopPx || 0);
    const pdfBottom = Number(editor.dataset.pdfBottomPx || wrapperHeight);
    const wrapperTop = Number(editor.dataset.wrapperTopPx || 0);
    const originalTop = pdfTop - wrapperTop - COVER_PAD_Y;
    const originalBottom = pdfBottom - wrapperTop + COVER_PAD_Y;
    const coverTop = Math.min(COVER_PAD_Y, originalTop);
    const coverBottom = Math.max(
        wrapperHeight - COVER_PAD_Y,
        editorHeight,
        originalBottom,
    );

    cover.style.left = `${coverLeft}px`;
    cover.style.top = `${coverTop}px`;
    cover.style.width = `${Math.max(coverRight - coverLeft, MIN_TEXT_WIDTH)}px`;
    cover.style.height = `${Math.max(coverBottom - coverTop, 1)}px`;
}

// Обновляет визуальное состояние блока: редактирование, изменение и сдвиг.
function updateEditorVisualState(editor) {
    const wrapper = editor.parentElement;
    const changed = editorHasChanges(editor);
    const moved = editorMoved(editor);
    const editing = document.activeElement === editor;

    editor.classList.toggle("changed", changed);

    if (wrapper) {
        wrapper.classList.toggle("changed", changed);
        wrapper.classList.toggle("moved", moved);
        wrapper.classList.toggle("editing", editing);
    }

    if (editing || changed || moved) {
        setEditorVisibility(editor, true, true);
    } else {
        setEditorVisibility(editor, false);
    }

    resizeBlockToText(editor);
    updateCoverGeometry(editor);
}

// Проверяет, находятся ли два блока на одной визуальной строке.
function sameVisualLine(a, b) {
    return (
        Math.abs(Number(a.dataset.originY) - Number(b.dataset.originY)) < 2.5
    );
}

// Проверяет, совпадает ли стиль двух PDF-спанов.
function sameStyle(a, b) {
    return (
        (a.dataset.font || "") === (b.dataset.font || "") &&
        (a.dataset.size || "") === (b.dataset.size || "") &&
        (a.dataset.flags || "") === (b.dataset.flags || "") &&
        (a.dataset.color || "") === (b.dataset.color || "")
    );
}

// Сохраняет динамическую позицию блока и двигает его относительно исходной точки.
function setDynamicLeft(editor, leftPx) {
    const originalLeft = Number(editor.dataset.leftPx || 0);
    editor.dataset.dynamicLeftPx = String(leftPx);
    editor.style.left = `${leftPx - originalLeft}px`;
}

// Возвращает последующие блоки цепочки на исходные позиции.
function resetChainFrom(editor) {
    let current = getNextEditor(editor);
    while (current) {
        const originalLeft = Number(current.dataset.leftPx || 0);
        setDynamicLeft(current, originalLeft);
        if (!editorHasChanges(current) && document.activeElement !== current) {
            current.style.width = `${current.dataset.originalWidth}px`;
        }
        current = getNextEditor(current);
    }
}

// Считает исходный промежуток между двумя соседними блоками.
function getOriginalGapBetweenEditors(current, next) {
    const currentOriginalLeft = Number(current.dataset.leftPx || 0);
    const currentOriginalWidth = Number(
        current.dataset.originalWidth || MIN_TEXT_WIDTH,
    );
    const nextOriginalLeft = Number(next.dataset.leftPx || 0);
    return Math.max(
        nextOriginalLeft - (currentOriginalLeft + currentOriginalWidth),
        MIN_BLOCK_GAP,
    );
}

// Сдвигает следующие блоки только при наложении или при сжатии обычного текста.
function pushNextEditors(editor) {
    const next = getNextEditor(editor);
    if (!next) return true;

    const currentLeft = Number(
        editor.dataset.dynamicLeftPx || editor.dataset.leftPx || 0,
    );
    const currentWidth = Math.max(
        parseFloat(editor.style.width || editor.dataset.originalWidth || "0"),
        getRequiredTextWidth(editor),
    );
    const currentOriginalWidth = Number(
        editor.dataset.originalWidth || MIN_TEXT_WIDTH,
    );
    const nextOriginalLeft = Number(next.dataset.leftPx || 0);
    const originalGap = getOriginalGapBetweenEditors(editor, next);
    const currentRight = currentLeft + currentWidth;
    const overlapsNext = currentRight + MIN_BLOCK_GAP > nextOriginalLeft;
    const isFlowTextPair = originalGap <= MAX_FLOW_GAP;
    const currentBecameShorter = currentWidth < currentOriginalWidth;

    let desiredNextLeft = nextOriginalLeft;

    if (isFlowTextPair && currentBecameShorter) {
        desiredNextLeft = currentRight + originalGap;
    } else if (overlapsNext) {
        desiredNextLeft = currentRight + MIN_BLOCK_GAP;
    }

    const appliedNextLeft = desiredNextLeft;

    const pageWidthPx = Number(next.dataset.pageWidthPx || 0);
    const nextOwnWidth = Math.max(
        parseFloat(next.style.width || next.dataset.originalWidth || "0"),
        getRequiredTextWidth(next),
    );
    const nextMaxLeft =
        pageWidthPx > 0 ? pageWidthPx - nextOwnWidth - 2 : appliedNextLeft;

    if (appliedNextLeft > nextMaxLeft) {
        return false;
    }

    setDynamicLeft(next, appliedNextLeft);

    return pushNextEditors(next);
}

// Пересчитывает всю цепочку блоков после текущего блока.
function recomputeFlowFrom(editor) {
    resetChainFrom(editor);
    return pushNextEditors(editor);
}

// Обновляет размеры и визуальное состояние всей цепочки блоков.
function updateChainVisuals(editor) {
    let current = editor;
    while (current) {
        resizeBlockToText(current);
        updateEditorVisualState(current);
        current = getNextEditor(current);
    }
}

// Сохраняет состояние цепочки, чтобы можно было откатить неудачную правку.
function snapshotChain(editor) {
    const snapshot = [];
    let current = editor;
    while (current) {
        snapshot.push({
            id: current.dataset.editorId,
            text: editorText(current),
            prevText: current.dataset.prevText,
            width: current.style.width,
            dynamicLeftPx: current.dataset.dynamicLeftPx,
        });
        current = getNextEditor(current);
    }
    return snapshot;
}

// Восстанавливает ранее сохраненное состояние цепочки блоков.
function restoreChain(snapshot) {
    snapshot.forEach((item) => {
        const editor = document.querySelector(
            `.overlay-text[data-editor-id="${item.id}"]`,
        );
        if (!editor) return;

        editor.textContent = item.text;
        editor.dataset.prevText = item.prevText;
        editor.style.width = item.width;
        editor.dataset.dynamicLeftPx = item.dynamicLeftPx;

        const originalLeft = Number(editor.dataset.leftPx || 0);
        const dynamicLeft = Number(item.dynamicLeftPx || originalLeft);
        editor.style.left = `${dynamicLeft - originalLeft}px`;
    });

    snapshot.forEach((item) => {
        const editor = document.querySelector(
            `.overlay-text[data-editor-id="${item.id}"]`,
        );
        if (!editor) return;

        updateEditorVisualState(editor);
    });
}

// Применяет новый текст к блоку и проверяет, помещается ли цепочка.
function tryApplyEditorChange(editor, nextText, options = {}) {
    const snapshot = snapshotChain(editor);

    editor.textContent = nextText;
    const ownTextFits = resizeBlockToText(editor, { allowPush: true });

    if (!ownTextFits && !options.allowOverflowRecovery) {
        restoreChain(snapshot);
        return false;
    }

    const ok = recomputeFlowFrom(editor);
    if (!ok && !options.allowOverflowRecovery) {
        restoreChain(snapshot);
        return false;
    }

    if (!ok) {
        resetChainFrom(editor);
    }

    editor.dataset.prevText = editorText(editor);
    updateChainVisuals(editor);
    return true;
}

// Вставляет текст из буфера в текущую позицию курсора.
function insertTextIntoEditor(editor, incomingText) {
    const currentText = editorText(editor);
    const { start, end } = getSelectionOffsets(editor);
    const cleanText = (incomingText || "").replace(/\r?\n/g, " ");

    if (!cleanText) {
        setCursorOffset(editor, start);
        return true;
    }

    const predictedText = buildPredictedText(
        currentText,
        cleanText,
        start,
        end,
    );

    if (tryApplyEditorChange(editor, predictedText)) {
        setCursorOffset(editor, start + cleanText.length);
        updateEditorVisualState(editor);
        return true;
    }

    setCursorOffset(editor, start);
    return false;
}

// Создает редактируемый HTML-блок поверх текста PDF.
function createOverlayDiv(unit, pageData, pageWidthPx) {
    const wrapper = document.createElement("div");
    wrapper.className = "overlay-wrapper";

    const cover = document.createElement("div");
    cover.className = "overlay-cover";

    const editor = document.createElement("div");
    editor.className = "overlay-text overlay-hidden";
    editor.contentEditable = "true";
    editor.spellcheck = false;
    editor.textContent = unit.text;
    editor.dataset.editorId = crypto.randomUUID();

    const left = unit.x0 * scale;
    const top = (unit.originY - unit.size * HTML_BASELINE_RATIO) * scale;

    const targetWidth = Math.max((unit.x1 - unit.x0) * scale, MIN_TEXT_WIDTH);
    const baseWidth = Math.max(targetWidth + TEXT_WIDTH_PAD, 20);
    const baseHeight = Math.max(unit.size * scale * 1.35, 20);
    const maxWidth = Math.max(pageWidthPx - left - 2, baseWidth);

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
    editor.dataset.pageWidthPx = pageWidthPx;

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
    editor.dataset.wrapperTopPx = top;
    editor.dataset.pdfTopPx = unit.y0 * scale;
    editor.dataset.pdfBottomPx = unit.y1 * scale;
    editor.dataset.leftPx = left;
    editor.dataset.dynamicLeftPx = left;

    editor.addEventListener("focus", () => {
        wrapper.classList.add("editing");
        setEditorVisibility(editor, true, true);
        resizeBlockToText(editor);
        recomputeFlowFrom(editor);
        updateEditorVisualState(editor);
    });

    editor.addEventListener("blur", () => {
        wrapper.classList.remove("editing");
        if (!editorHasChanges(editor)) {
            editor.style.width = `${editor.dataset.originalWidth}px`;
            recomputeFlowFrom(editor);
        }
        updateEditorVisualState(editor);
    });

    editor.addEventListener("input", () => {
        const oldPrev = editor.dataset.prevText || editor.dataset.oldText || "";
        const current = editorText(editor);

        if (!tryApplyEditorChange(editor, current)) {
            editor.textContent = oldPrev;
            placeCursorAtEnd(editor);
            tryApplyEditorChange(editor, oldPrev);
            return;
        }

        updateEditorVisualState(editor);
    });

    editor.addEventListener("beforeinput", (e) => {
        const inputType = e.inputType || "";

        if (
            inputType === "insertParagraph" ||
            inputType === "insertLineBreak"
        ) {
            e.preventDefault();
            return;
        }

        if (
            inputType === "deleteContentBackward" ||
            inputType === "deleteContentForward" ||
            inputType === "deleteByCut" ||
            inputType === "deleteByDrag"
        ) {
            const currentText = editorText(editor);
            const { start, end } = getSelectionOffsets(editor);
            const direction =
                inputType === "deleteContentForward" ? "forward" : "backward";
            const deleted = buildDeletedText(
                currentText,
                start,
                end,
                direction,
            );

            e.preventDefault();

            if (deleted.text === currentText) {
                setCursorOffset(editor, deleted.cursor);
                return;
            }

            if (
                tryApplyEditorChange(editor, deleted.text, {
                    allowOverflowRecovery: true,
                })
            ) {
                setCursorOffset(editor, deleted.cursor);
                updateEditorVisualState(editor);
            }

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
                incomingText =
                    (e.clipboardData || window.clipboardData)?.getData(
                        "text",
                    ) || "";
            }

            incomingText = (incomingText || "").replace(/\r?\n/g, " ");

            e.preventDefault();

            if (!incomingText) {
                setCursorOffset(editor, start);
                return;
            }

            const predictedText = buildPredictedText(
                currentText,
                incomingText,
                start,
                end,
            );

            if (tryApplyEditorChange(editor, predictedText)) {
                setCursorOffset(editor, start + incomingText.length);
                updateEditorVisualState(editor);
            } else {
                setCursorOffset(editor, start);
            }

            return;
        }
    });

    editor.addEventListener("copy", (e) => {
        const selectedText = getSelectedTextInside(editor);
        if (!selectedText) return;

        e.preventDefault();
        e.clipboardData.setData("text/plain", selectedText);
    });

    editor.addEventListener("paste", (e) => {
        e.preventDefault();
        const pastedText =
            (e.clipboardData || window.clipboardData)?.getData("text/plain") ||
            "";
        insertTextIntoEditor(editor, pastedText);
    });

    wrapper.addEventListener("mousedown", (event) => {
        if (event.target === editor) {
            setEditorVisibility(editor, true, true);
            return;
        }

        if (document.activeElement !== editor) {
            setEditorVisibility(editor, true, true);
            requestAnimationFrame(() => {
                editor.focus();
            });
        }
    });

    wrapper.appendChild(cover);
    wrapper.appendChild(editor);
    updateEditorVisualState(editor);
    return wrapper;
}

// Связывает соседние текстовые блоки на одной строке для сдвига вправо.
function linkEditorsOnPage(pageWrapper) {
    const editors = Array.from(pageWrapper.querySelectorAll(".overlay-text"));

    editors.sort((a, b) => {
        const ay = Number(a.dataset.originY);
        const by = Number(b.dataset.originY);
        if (Math.abs(ay - by) > 2.5) return ay - by;
        return Number(a.dataset.x0) - Number(b.dataset.x0);
    });

    for (let i = 0; i < editors.length - 1; i++) {
        const current = editors[i];
        const next = editors[i + 1];

        if (!sameVisualLine(current, next)) continue;

        current.dataset.nextEditorId = next.dataset.editorId;
    }
}

// Загружает выбранный PDF на сервер и открывает его в редакторе.
async function openPdf() {
    const file = pdfFileInput.files[0];
    if (!file) {
        fileNameEl.textContent = "PDF súbor nevybraný";
        setStatus("Vyberte PDF súbor.", true);
        return;
    }
    try {
        fileNameEl.textContent = file.name;
        setStatus("Načítavam PDF...");
        applyBtn.disabled = true;
        viewer.innerHTML = "";

        const formData = new FormData();
        formData.append("pdf", file);

        const response = await fetch(appUrl("api/pdf/open"), {
            method: "POST",
            body: formData,
        });

        const data = await response.json();

        if (!response.ok || data.error) {
            throw new Error(data.error || "Chyba pri otvorení PDF.");
        }

        currentFilename = data.filename;
        currentPages = data.pages || [];
        currentPdfUrl = appUrl(data.pdfUrl);

        await registerFontFaces(data.fontFaces || []);
        await renderPdfWithOverlay(currentPdfUrl, currentPages);

        applyBtn.disabled = false;
        setStatus("PDF bol načítaný.");
    } catch (error) {
        console.error(error);
        setStatus(error.message || "Chyba pri otvorení PDF.", true);
    }
}

// Пересчитывает размеры всех текстовых блоков после загрузки страницы.
function resizeAllOverlayTexts() {
    const editors = document.querySelectorAll(".overlay-text");

    editors.forEach((editor) => {
        if (!editorHasChanges(editor) && document.activeElement !== editor) {
            editor.style.width = `${editor.dataset.originalWidth}px`;
        }
        updateEditorVisualState(editor);
    });
}

// Рендерит PDF через pdf.js и накладывает редактируемый HTML-слой.
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
            overlayLayer.appendChild(
                createOverlayDiv(unit, pageData, viewport.width),
            );
        }

        pageWrapper.appendChild(overlayLayer);
        viewer.appendChild(pageWrapper);

        linkEditorsOnPage(pageWrapper);
    }

    resizeAllOverlayTexts();
}

// Собирает список измененных или сдвинутых блоков для сохранения в PDF.
function collectChanges() {
    const editors = Array.from(document.querySelectorAll(".overlay-text"));
    const changes = [];

    for (const el of editors) {
        const oldText = el.dataset.oldText || "";
        const newText = editorText(el);

        const originalLeftPx = Number(el.dataset.leftPx || 0);
        const dynamicLeftPx = Number(
            el.dataset.dynamicLeftPx || originalLeftPx,
        );
        const currentWidthPx = parseFloat(
            el.style.width || el.dataset.originalWidth || "0",
        );
        const currentRightPdf = (dynamicLeftPx + currentWidthPx) / scale;

        const moved = Math.abs(dynamicLeftPx - originalLeftPx) > 0.5;
        const textChanged = normalize(oldText) !== normalize(newText);

        if (!moved && !textChanged) {
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
            maxX: currentRightPdf,
            font: el.dataset.font || "",
            size: Number(el.dataset.size),
            color: Number(el.dataset.color),
            flags: Number(el.dataset.flags),
            originY: Number(el.dataset.originY),

            drawX: dynamicLeftPx / scale,
            currentX1: currentRightPdf,
            movedOnly: moved && !textChanged,
        });
    }

    return changes;
}

// Отправляет изменения на сервер и скачивает новый PDF.
async function applyPdfChanges() {
    if (!currentFilename) {
        alert("Najprv nahrajte PDF.");
        return;
    }
    try {
        const changes = collectChanges();

        if (changes.length === 0) {
            alert("Nie sú žiadne zmeny na uloženie.");
            return;
        }

        setStatus("Ukladám PDF...");
        applyBtn.disabled = true;

        const response = await fetch(appUrl("api/pdf/apply"), {
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
            throw new Error(data.error || "Chyba pri ukladaní PDF.");
        }

        setStatus("PDF bol uložený.");
        window.location.href = appUrl(data.downloadUrl);
    } catch (error) {
        console.error(error);
        setStatus(error.message || "Chyba pri ukladaní PDF.", true);
    } finally {
        applyBtn.disabled = false;
    }
}
