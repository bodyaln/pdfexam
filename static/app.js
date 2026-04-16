let currentFilename = null;
let currentPages = [];
let currentPdfUrl = null;
const scale = 1.5;

pdfjsLib.GlobalWorkerOptions.workerSrc =
    "https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.worker.min.js";

const pdfFileInput = document.getElementById("pdfFile");
const uploadBtn = document.getElementById("uploadBtn");
const saveBtn = document.getElementById("saveBtn");
const viewer = document.getElementById("viewer");
const statusEl = document.getElementById("status");

uploadBtn.addEventListener("click", uploadPdf);
saveBtn.addEventListener("click", savePdf);

function setStatus(text, isError = false) {
    statusEl.textContent = text;
    statusEl.classList.toggle("status-error", isError);
}

function normalize(text) {
    return (text || "").replace(/\u00a0/g, " ").trim();
}

async function registerFontFaces(fontFaces) {
    let styleTag = document.getElementById("dynamic-font-faces");

    if (!styleTag) {
        styleTag = document.createElement("style");
        styleTag.id = "dynamic-font-faces";
        document.head.appendChild(styleTag);
    }

    const css = (fontFaces || [])
        .map((font) => {
            return `
@font-face {
  font-family: "${font.fontFamily}";
  src: url("${font.url}");
}
`;
        })
        .join("\n");

    styleTag.textContent = css;

    if (document.fonts && document.fonts.ready) {
        try {
            await document.fonts.ready;
        } catch (e) {
            console.warn("Fonts ready wait failed", e);
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
        const textLength = element.innerText.length;
        return { start: textLength, end: textLength };
    }

    const range = selection.getRangeAt(0);

    if (
        !element.contains(range.startContainer) ||
        !element.contains(range.endContainer)
    ) {
        const textLength = element.innerText.length;
        return { start: textLength, end: textLength };
    }

    const preStartRange = range.cloneRange();
    preStartRange.selectNodeContents(element);
    preStartRange.setEnd(range.startContainer, range.startOffset);
    const start = preStartRange.toString().length;

    const preEndRange = range.cloneRange();
    preEndRange.selectNodeContents(element);
    preEndRange.setEnd(range.endContainer, range.endOffset);
    const end = preEndRange.toString().length;

    return { start, end };
}

function buildPredictedText(currentText, insertText, start, end) {
    return currentText.slice(0, start) + insertText + currentText.slice(end);
}

function getMaxAllowedWidth(el) {
    const wrapper = el.parentElement;
    if (!wrapper) return 8;

    return Math.max(wrapper.clientWidth - 2, 8);
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
        measurer.style.padding = "0";
        measurer.style.margin = "0";
        measurer.style.border = "0";
        document.body.appendChild(measurer);
    }

    return measurer;
}

function measureTextWidthForElement(el, text) {
    const measurer = getTextMeasurer();

    measurer.style.fontFamily = el.style.fontFamily;
    measurer.style.fontSize = el.style.fontSize;
    measurer.style.fontStyle = el.style.fontStyle;
    measurer.style.fontWeight = el.style.fontWeight;
    measurer.style.lineHeight = el.style.lineHeight || "1.2";
    measurer.textContent = text || "";

    return Math.ceil(measurer.getBoundingClientRect().width) + 10;
}

function resizeBlockToText(el) {
    const text = el.innerText || "";
    const measuredWidth = measureTextWidthForElement(el, text);
    const maxAllowedWidth = getMaxAllowedWidth(el);
    const finalWidth = Math.min(Math.max(measuredWidth, 8), maxAllowedWidth);

    el.style.width = `${finalWidth}px`;
    return measuredWidth <= maxAllowedWidth;
}

function fitsTextInBlock(el, candidateText) {
    const measuredWidth = measureTextWidthForElement(el, candidateText);
    const maxAllowedWidth = getMaxAllowedWidth(el);
    return measuredWidth <= maxAllowedWidth;
}

function createOverlayDiv(unit, pageData) {
    const wrapper = document.createElement("div");
    wrapper.className = "overlay-wrapper";

    const cover = document.createElement("div");
    cover.className = "overlay-cover";

    const editor = document.createElement("div");
    editor.className = "overlay-text";
    editor.contentEditable = "true";
    editor.spellcheck = false;
    editor.innerText = unit.text;

    const left = unit.x0 * scale;
    const top = unit.y0 * scale - 1;
    const baseWidth = Math.max((unit.x1 - unit.x0) * scale + 4, 20);
    const baseHeight = Math.max(
        (unit.y1 - unit.y0) * scale + 4,
        unit.size * scale * 1.25,
    );

    const maxX = (unit.maxX ?? pageData.width) * scale;
    const coverWidth = Math.max(maxX - left, baseWidth);

    wrapper.style.position = "absolute";
    wrapper.style.left = `${left}px`;
    wrapper.style.top = `${top}px`;
    wrapper.style.width = `${coverWidth}px`;
    wrapper.style.height = `${baseHeight}px`;
    wrapper.style.overflow = "hidden";

    cover.style.position = "absolute";
    cover.style.left = "0px";
    cover.style.top = "0px";
    cover.style.width = `${coverWidth}px`;
    cover.style.height = `${baseHeight}px`;
    cover.style.background = "#ffffff";

    editor.style.position = "absolute";
    editor.style.left = "0px";
    editor.style.top = "0px";
    editor.style.width = `${baseWidth}px`;
    editor.style.minWidth = "8px";
    editor.style.height = `${baseHeight}px`;
    editor.style.fontSize = `${unit.size * scale}px`;

    editor.style.fontFamily =
        unit.browserFont || "Arial Unicode MS, Arial, sans-serif";

    editor.style.fontStyle = unit.isItalic ? "italic" : "normal";
    editor.style.fontWeight = unit.isBold ? "bold" : "normal";

    editor.dataset.unitId = unit.id;
    editor.dataset.page = unit.page;
    editor.dataset.pageWidth = pageData.width;
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

    resizeBlockToText(editor);
    editor.addEventListener("focus", () => {
        resizeBlockToText(editor);
    });
    editor.addEventListener("input", () => {
        resizeBlockToText(editor);
        editor.dataset.prevText = editor.innerText;

        const changed =
            normalize(editor.innerText) !== normalize(editor.dataset.oldText);

        editor.classList.toggle("changed", changed);
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
            const currentText = editor.innerText || "";
            const { start, end } = getSelectionOffsets(editor);

            let incomingText = e.data ?? "";

            if (inputType === "insertFromPaste") {
                incomingText =
                    (e.clipboardData || window.clipboardData)?.getData(
                        "text",
                    ) || "";
            }

            const predictedText = buildPredictedText(
                currentText,
                incomingText,
                start,
                end,
            );

            const fits = fitsTextInBlock(editor, predictedText);

            if (!fits) {
                e.preventDefault();
                placeCursorAtEnd(editor);
            }
        }
    });

    wrapper.appendChild(cover);
    wrapper.appendChild(editor);
    return wrapper;
}
async function uploadPdf() {
    const file = pdfFileInput.files[0];

    if (!file) {
        alert("Выбери PDF файл");
        return;
    }

    try {
        setStatus("Загрузка PDF...");
        saveBtn.disabled = true;
        viewer.innerHTML = "";

        const formData = new FormData();
        formData.append("pdf", file);

        const response = await fetch("/upload", {
            method: "POST",
            body: formData,
        });

        const data = await response.json();

        if (!response.ok || data.error) {
            throw new Error(data.error || "Ошибка загрузки PDF");
        }

        currentFilename = data.filename;
        currentPages = data.pages || [];
        currentPdfUrl = data.pdfUrl;

        await registerFontFaces(data.fontFaces || []);
        await renderPdfWithOverlay(currentPdfUrl, currentPages);

        saveBtn.disabled = false;
        setStatus("PDF загружен");
    } catch (error) {
        console.error(error);
        setStatus(error.message || "Ошибка загрузки PDF", true);
    }
}
function resizeAllOverlayTexts() {
    const editors = document.querySelectorAll(".overlay-text");
    editors.forEach((editor) => {
        resizeBlockToText(editor);
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
            const div = createOverlayDiv(unit, pageData);
            overlayLayer.appendChild(div);
        }

        pageWrapper.appendChild(overlayLayer);
        viewer.appendChild(pageWrapper);
    }
    resizeAllOverlayTexts();
}

function collectChanges() {
    const elements = document.querySelectorAll(".overlay-text");
    const changes = [];

    elements.forEach((el) => {
        const oldText = el.dataset.oldText || "";
        const newText = el.innerText || "";

        if (normalize(oldText) === normalize(newText)) {
            return;
        }

        changes.push({
            unitId: el.dataset.unitId,
            page: Number(el.dataset.page),
            oldText,
            newText,
            x0: Number(el.dataset.x0),
            y0: Number(el.dataset.y0),
            x1: Number(el.dataset.x1),
            y1: Number(el.dataset.y1),
            maxX: Number(el.dataset.maxX),
            font: el.dataset.font,
            size: Number(el.dataset.size),
            color: Number(el.dataset.color),
            flags: Number(el.dataset.flags),
            isItalic: el.dataset.isItalic === "1",
            isBold: el.dataset.isBold === "1",
        });
    });

    return changes;
}

async function savePdf() {
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
        saveBtn.disabled = true;

        const response = await fetch("/save", {
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

        setStatus("PDF сохранён, начинается загрузка");
        window.location.href = data.downloadUrl;
    } catch (error) {
        console.error(error);
        setStatus(error.message || "Ошибка сохранения PDF", true);
    } finally {
        saveBtn.disabled = false;
    }
}
