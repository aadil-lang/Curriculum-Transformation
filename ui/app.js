const state = {
  currentBatch: null,
  documents: [],
  sampleCsvFile: null,
  samplePreviewSelection: { ref: "A1", value: "" },
  samplePreviewRows: [],
  cleanCsv: "",
  cleanCsvName: "extracted.csv",
  showFinal: false,
  isBusy: false,
  batchPollTimerId: null,
  batchPollName: "",
  batchCapabilities: {
    hasFinalCsv: false,
    hasSampleArtifact: false,
  },
};

const BUTTON_SUCCESS_MS = 2200;
const MIN_BUTTON_LOADING_MS = 500;
const managedButtons = [];

const els = {
  documentFiles: document.getElementById("documentFiles"),
  sourceUrls: document.getElementById("sourceUrls"),
  sampleCsvFile: document.getElementById("sampleCsvFile"),
  sampleCsvEditor: document.getElementById("sampleCsvEditor"),
  // The sheet toolbar (title/pill/meta) was removed; keep no-op stubs so the render
  // code that still writes to these targets stays harmless without null checks.
  samplePreviewTitle: document.getElementById("samplePreviewTitle") || {},
  samplePreviewMeta: document.getElementById("samplePreviewMeta") || {},
  samplePreviewCellRef: document.getElementById("samplePreviewCellRef"),
  samplePreviewFormula: document.getElementById("samplePreviewFormula"),
  samplePreviewTableWrap: document.getElementById("samplePreviewTableWrap"),
  statusBox: document.getElementById("statusBox"),
  schemaPathBox: document.getElementById("schemaPathBox"),
  samplePathBox: document.getElementById("samplePathBox"),
  resultSummary: document.getElementById("resultSummary"),
  previewHeading: document.getElementById("previewHeading"),
  previewPill: document.getElementById("previewPill") || {},
  actionBox: document.getElementById("actionBox"),
  batchBadge: document.getElementById("batchBadge"),
  generateDraftButton: document.getElementById("generateDraftButton"),
  runExtractionButton: document.getElementById("runExtractionButton"),
  approveDraftButton: document.getElementById("approveDraftButton"),
  downloadFinalButton: document.getElementById("downloadFinalButton"),
  formPanelTitle: document.getElementById("formPanelTitle"),
  tabExtraction: document.getElementById("tabExtraction"),
  tabReview: document.getElementById("tabReview"),
  extractionTab: document.getElementById("extractionTab"),
  reviewTab: document.getElementById("reviewTab"),
  reviewCsvFile: document.getElementById("reviewCsvFile"),
  reviewSourceFile: document.getElementById("reviewSourceFile"),
  reviewSourceUrl: document.getElementById("reviewSourceUrl"),
  reviewModeBatch: document.getElementById("reviewModeBatch"),
  reviewModeExternal: document.getElementById("reviewModeExternal"),
  reviewBatchMode: document.getElementById("reviewBatchMode"),
  reviewExternalMode: document.getElementById("reviewExternalMode"),
  reviewBatchSelect: document.getElementById("reviewBatchSelect"),
  reviewCsvButton: document.getElementById("reviewCsvButton"),
  reviewInstructions: document.getElementById("reviewInstructions"),
  reviewSuggestions: document.getElementById("reviewSuggestions"),
  reviewSummary: document.getElementById("reviewSummary"),
  reviewFindings: document.getElementById("reviewFindings"),
  reviewFixRow: document.getElementById("reviewFixRow"),
  approveFixButton: document.getElementById("approveFixButton"),
  downloadFixedButton: document.getElementById("downloadFixedButton"),
  reviewFixSummary: document.getElementById("reviewFixSummary"),
};

const reviewState = { mode: "batch", csvFile: null, sourceFile: null, csvText: "", findings: [], correctedCsv: "", correctedName: "", batchesLoaded: false };

els.documentFiles.addEventListener("change", async (event) => {
  state.documents = Array.from(event.target.files || []);
});

els.sampleCsvEditor.addEventListener("input", () => {
  renderSamplePreview(els.sampleCsvEditor.value);
  updateButtonAvailability();
});

els.sampleCsvFile.addEventListener("change", async (event) => {
  const file = Array.from(event.target.files || [])[0] || null;
  state.sampleCsvFile = file;
  if (file) {
    els.sampleCsvEditor.value = await file.text();
  }
  updateButtonAvailability();
});

els.generateDraftButton.addEventListener("click", async () => {
  const hasSource =
    (state.documents && state.documents.length > 0) ||
    collectSourceUrls().length > 0 ||
    Boolean(state.currentBatch);
  if (!hasSource) {
    setStatus("Add at least one source document or URL to draft a sample CSV.");
    return;
  }

  setStatus("Generating a draft sample CSV...");
  try {
    await runButtonAction(els.generateDraftButton, async () => {
      const response = await postJson("/api/draft-sample", {
        name: state.currentBatch || "",
        document_files: await encodeFiles(state.documents),
        source_urls: collectSourceUrls(),
      });
      hydrateBatch(response.batch);
      setStatus(
        response.result?.queued
          ? `${response.result.message} Job id: ${response.result.job_id}`
          : summarizeDraftResult(response),
      );
      await refreshWorkspace();
    });
  } catch (error) {
    setStatus(error.message);
  }
});

els.runExtractionButton.addEventListener("click", async () => {
  await runExtractionFromEditorOrUpload(els.runExtractionButton);
});

els.approveDraftButton.addEventListener("click", async () => {
  const sampleCsvContent = els.sampleCsvEditor.value.trim();
  if (!sampleCsvContent) {
    setStatus("Generate a draft sample before approving.");
    return;
  }
  setStatus("Approving draft sample...");
  try {
    await runButtonAction(els.approveDraftButton, async () => {
      const response = await postJson("/api/approve-sample", {
        name: state.currentBatch || "",
        sample_csv_name: state.sampleCsvFile ? state.sampleCsvFile.name : "approved_sample.csv",
        sample_csv_content: sampleCsvContent,
        document_files: await encodeFiles(state.documents),
        source_urls: collectSourceUrls(),
      });
      hydrateBatch(response.batch);
      setStatus(response.result?.message || "Sample approved. Hit Extract to run the full extraction.");
      await refreshWorkspace();
    });
  } catch (error) {
    setStatus(error.message);
  }
});

els.downloadFinalButton.addEventListener("click", () => {
  downloadFinalCsv();
});

async function runExtractionFromEditorOrUpload(triggerButton) {
  const sampleCsvContent = els.sampleCsvEditor.value.trim();
  if (!sampleCsvContent) {
    setStatus("Approve or upload a sample CSV before extraction.");
    return;
  }
  if (!state.documents.length && !collectSourceUrls().length) {
    setStatus("Add a source document or URL before extraction.");
    return;
  }

  setStatus("Running extraction...");
  const stopProgress = startExtractionProgressPolling(state.currentBatch);
  try {
    await runButtonAction(triggerButton, async () => {
      const response = await postJson("/api/run-extraction", {
        name: state.currentBatch || "",
        sample_csv_name: state.sampleCsvFile ? state.sampleCsvFile.name : "approved_sample.csv",
        sample_csv_content: sampleCsvContent,
        document_files: await encodeFiles(state.documents),
        source_urls: collectSourceUrls(),
      });
      stopProgress();
      hydrateBatch(response.batch);
      setStatus(
        response.result?.queued
          ? `${response.result.message} Job id: ${response.result.job_id}`
          : summarizeExtractionResult(response.result),
      );
      await refreshWorkspace();
    });
  } catch (error) {
    setStatus(error.message);
  } finally {
    stopProgress();
  }
}

// Human-readable labels for the pipeline's extraction_progress.json stages.
const EXTRACTION_STAGE_LABELS = {
  starting: "Preparing extraction",
  parsing: "Reading the document",
  extracting: "Analyzing document and extracting rows",
  reviewing: "Reviewing and validating rows",
  finalizing: "Finalizing the CSV",
  failed: "Extraction failed",
};

// While the (synchronous) run-extraction request is in flight, poll the batch detail
// endpoint for the pipeline's live progress marker and render it as the status line.
// Returns a stop() function; safe to call stop() more than once.
function startExtractionProgressPolling(batchName) {
  if (!batchName) {
    return () => {};
  }
  let stopped = false;
  let inFlight = false;

  const tick = async () => {
    if (stopped || inFlight) {
      return;
    }
    inFlight = true;
    try {
      const detail = await getJson(`/api/batches/${encodeURIComponent(batchName)}`);
      const progress = detail && detail.extraction_progress;
      if (!stopped && progress && progress.stage) {
        const label = EXTRACTION_STAGE_LABELS[progress.stage] || progress.stage;
        let line = label;
        if (progress.rows_done != null && progress.rows_target) {
          line += ` — ${progress.rows_done}/${progress.rows_target} rows`;
        } else if (progress.rows_done != null) {
          line += ` — ${progress.rows_done} rows`;
        }
        if (progress.detail) {
          line += `\n${progress.detail}`;
        }
        setStatus(line);
      }
    } catch (error) {
      /* transient poll failure is non-fatal; keep polling */
    } finally {
      inFlight = false;
    }
  };

  const timerId = setInterval(tick, 2000);
  tick();
  return () => {
    stopped = true;
    clearInterval(timerId);
  };
}

// Build a short human-readable completion line from a run-extraction result, instead of
// dumping the raw JSON. Prefers the per-document message (which carries the row count +
// coverage), falling back to a generic "Extraction completed."
function summarizeExtractionResult(result) {
  if (!result) {
    return "Extraction completed.";
  }
  const docResults = Array.isArray(result.results) ? result.results : [];
  const failed = docResults.filter((r) => r && r.status && r.status !== "verified");
  const primary = docResults.find((r) => r && r.status === "verified") || docResults[0];
  const detail = primary && primary.message ? ` ${primary.message}` : "";
  if (failed.length) {
    return `Extraction completed with ${failed.length} document(s) needing review.${detail}`;
  }
  return `Extraction completed.${detail}`;
}

// Short completion line for a draft-sample run. Prefers the drafted row count from the
// batch's sample preview; falls back to a generic message.
function summarizeDraftResult(response) {
  const rows = Array.isArray(state.samplePreviewRows) ? Math.max(state.samplePreviewRows.length - 1, 0) : 0;
  if (rows > 0) {
    return `Sample ready — ${rows} row(s). Review the preview, then approve.`;
  }
  return "Sample ready. Review the preview, then approve.";
}

function hydrateBatch(batch) {
  state.currentBatch = batch.name;
  state.batchCapabilities.hasFinalCsv = Boolean(batch.final_csv_path);
  state.batchCapabilities.hasSampleArtifact = Boolean(batch.approved_sample_csv_path || batch.sample_template_path);
  els.sampleCsvEditor.value = batch.approved_sample_csv || batch.sample_template_csv || "";
  els.schemaPathBox.textContent = batch.schema_config_path || "No schema yet.";
  els.samplePathBox.textContent = batch.approved_sample_csv_path || batch.sample_template_path || "No sample yet.";
  state.cleanCsv = batch.clean_csv || batch.final_csv || "";
  state.cleanCsvName = (batch.clean_csv_path || batch.final_csv_path || "").split(/[\\/]/).pop() || `${batch.name || "extracted"}.csv`;
  // Once extraction has produced output (or the batch has moved past drafting),
  // the shared preview shows the final CSV read-only; otherwise the editable sample.
  const extractedStatuses = ["extracted", "manual_review", "queued", "processing"];
  state.showFinal = Boolean(state.cleanCsv.trim() || batch.final_csv_path) || extractedStatuses.includes(batch.status || "");
  renderResultSummary(batch.row_summary, state.cleanCsv);
  els.actionBox.textContent = "";
  els.batchBadge.textContent = batch.status || "Idle";
  renderPreview();
  updateButtonAvailability();
}

function renderPreview() {
  if (state.showFinal) {
    els.previewHeading.textContent = "Extracted CSV";
    els.previewPill.textContent = "Result";
    els.samplePreviewTitle.textContent = state.cleanCsvName || "Extracted CSV";
    renderReadOnlyGrid(els.samplePreviewTableWrap, state.cleanCsv, els.samplePreviewMeta);
    els.resultSummary.hidden = false;
  } else {
    els.previewHeading.textContent = "Preview";
    els.previewPill.textContent = "CSV import";
    renderSamplePreview(els.sampleCsvEditor.value);
    els.resultSummary.hidden = true;
  }
}

async function refreshWorkspace() {
  updateButtonAvailability();
}

async function seedBlankSampleTemplate() {
  // On a fresh workspace (no batch loaded, empty editor), show a blank spreadsheet
  // of the default schema's columns so the user can build a sample CSV inline.
  if (state.currentBatch || state.showFinal || els.sampleCsvEditor.value.trim()) {
    return;
  }
  try {
    const workspace = await getJson("/api/workspace");
    const columns = workspace.default_schema_columns || [];
    if (!columns.length) {
      return;
    }
    els.sampleCsvEditor.value = columns.map(escapeCsvCell).join(",") + "\n";
    renderPreview();
    updateButtonAvailability();
  } catch (error) {
    // Non-fatal: leave the empty-state message if the workspace call fails.
  }
}

async function loadBatch(batchName) {
  setStatus(`Loading batch ${batchName}...`);
  try {
    const batch = await getJson(`/api/batches/${encodeURIComponent(batchName)}`);
    hydrateBatch(batch);
    setStatus(`Loaded batch ${batchName}.`);
  } catch (error) {
    setStatus(error.message);
  }
}

async function refreshActiveBatchSilently(batchName) {
  try {
    const batch = await getJson(`/api/batches/${encodeURIComponent(batchName)}`);
    hydrateBatch(batch);
  } catch (error) {
    setStatus(error.message);
    clearBatchPolling();
  }
}

function updateBatchPolling(batch) {
  const jobStatus = batch?.codex_job_status?.status || "";
  const shouldPoll = ["pending", "claimed", "running"].includes(jobStatus) || ["queued", "processing"].includes(batch?.status || "");
  if (!shouldPoll) {
    clearBatchPolling();
    return;
  }

  const activeBatchName = batch.name;
  if (state.batchPollTimerId && state.batchPollName === activeBatchName) {
    return;
  }
  clearBatchPolling();
  state.batchPollName = activeBatchName;
  state.batchPollTimerId = window.setInterval(() => {
    if (!state.isBusy && activeBatchName) {
      refreshActiveBatchSilently(activeBatchName);
    }
  }, 5000);
}

function clearBatchPolling() {
  if (state.batchPollTimerId) {
    window.clearInterval(state.batchPollTimerId);
    state.batchPollTimerId = null;
  }
  state.batchPollName = "";
}

function normalizedBatchName() {
  return state.currentBatch || "";
}

function collectSourceUrls() {
  return els.sourceUrls.value
    .split(/\s+/)
    .map((url) => url.trim())
    .filter((url) => /^https?:\/\//i.test(url));
}

function setStatus(message) {
  els.statusBox.textContent = message;
}

function setActionStatus(message) {
  els.actionBox.textContent = message;
}

function downloadTextAsFile(text, filename) {
  const blob = new Blob([text], { type: "text/csv;charset=utf-8" });
  const objectUrl = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = objectUrl;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(objectUrl);
}

function downloadCurrentSampleCsv() {
  const sampleCsvContent = els.sampleCsvEditor.value;
  if (!sampleCsvContent.trim()) {
    setStatus("No sample CSV is available to download yet.");
    return;
  }

  const filename = resolveSampleDownloadFilename();
  const blob = new Blob([sampleCsvContent], { type: "text/csv;charset=utf-8" });
  const objectUrl = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = objectUrl;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(objectUrl);
  setStatus(`Downloaded sample CSV as ${filename}.`);
}

function resolveSampleDownloadFilename() {
  const samplePath = els.samplePathBox.textContent.trim();
  if (samplePath && samplePath !== "No sample yet.") {
    const pieces = samplePath.split(/[\\/]/);
    const existingFilename = pieces[pieces.length - 1];
    if (existingFilename && existingFilename.endsWith(".csv")) {
      return existingFilename;
    }
  }

  const batchName = normalizedBatchName();
  if (batchName) {
    return `${batchName}-S.csv`;
  }

  return "sample-S.csv";
}

function renderSamplePreview(csvText) {
  const trimmed = csvText.trim();
  els.samplePreviewTitle.textContent = resolveSamplePreviewTitle();
  if (!trimmed) {
    state.samplePreviewRows = [];
    updateSamplePreviewSelection("A1", "");
    els.samplePreviewMeta.textContent = "No sample preview yet.";
    els.samplePreviewTableWrap.className = "sheet-grid empty-state";
    els.samplePreviewTableWrap.textContent = "No sample preview yet.";
    return;
  }

  try {
    const rows = parseCsv(trimmed);
    if (!rows.length) {
      state.samplePreviewRows = [];
      updateSamplePreviewSelection("A1", "");
      els.samplePreviewMeta.textContent = "No sample preview yet.";
      els.samplePreviewTableWrap.className = "sheet-grid empty-state";
      els.samplePreviewTableWrap.textContent = "No sample preview yet.";
      return;
    }

    const header = rows[0];
    const bodyRows = rows.slice(1);
    state.samplePreviewRows = rows.map((row) => [...row]);
    els.samplePreviewMeta.textContent = `${bodyRows.length} rows • ${header.length} columns`;
    els.samplePreviewTableWrap.className = "sheet-grid";
    els.samplePreviewTableWrap.innerHTML = "";

    const table = document.createElement("table");
    table.className = "sheet-table";
    table.style.width = `${totalTableWidth(header)}px`;

    const columnGroup = document.createElement("colgroup");
    const rowNumberCol = document.createElement("col");
    rowNumberCol.className = "sheet-row-index-col";
    rowNumberCol.style.width = `${ROW_INDEX_COL_WIDTH}px`;
    columnGroup.appendChild(rowNumberCol);
    for (const columnName of header) {
      const col = document.createElement("col");
      col.style.width = `${estimateColumnWidth(columnName)}px`;
      columnGroup.appendChild(col);
    }
    table.appendChild(columnGroup);

    const thead = document.createElement("thead");
    const lettersRow = document.createElement("tr");
    const corner = document.createElement("th");
    corner.className = "sheet-corner";
    corner.textContent = "";
    lettersRow.appendChild(corner);
    for (let index = 0; index < header.length; index += 1) {
      const th = document.createElement("th");
      th.className = "sheet-column-letter";
      th.textContent = spreadsheetColumnLabel(index);
      lettersRow.appendChild(th);
    }
    thead.appendChild(lettersRow);

    const tbody = document.createElement("tbody");
    const previewRows = [header, ...bodyRows];
    const visibleRowCount = Math.max(previewRows.length, 26);

    for (let rowIndex = 0; rowIndex < visibleRowCount; rowIndex += 1) {
      const tr = document.createElement("tr");

      const rowHeader = document.createElement("th");
      rowHeader.className = "sheet-row-number";
      rowHeader.textContent = String(rowIndex + 1);
      tr.appendChild(rowHeader);

      const sourceRow = previewRows[rowIndex] ? [...previewRows[rowIndex]] : [];
      while (sourceRow.length < header.length) {
        sourceRow.push("");
      }

      for (let columnIndex = 0; columnIndex < header.length; columnIndex += 1) {
        const cell = document.createElement("td");
        const cellValue = normalizeSheetCellText(sourceRow[columnIndex] || "");
        const cellRef = `${spreadsheetColumnLabel(columnIndex)}${rowIndex + 1}`;
        cell.className = rowIndex === 0 ? "sheet-cell sheet-header-value" : "sheet-cell";
        cell.textContent = cellValue;
        cell.contentEditable = "true";
        cell.spellcheck = false;
        cell.dataset.cellRef = cellRef;
        cell.dataset.cellValue = cellValue;
        cell.addEventListener("click", () => {
          updateSamplePreviewSelection(cellRef, cell.textContent || "");
          highlightSelectedPreviewCell(cellRef);
        });
        cell.addEventListener("focus", () => {
          updateSamplePreviewSelection(cellRef, cell.textContent || "");
          highlightSelectedPreviewCell(cellRef);
        });
        cell.addEventListener("input", () => {
          const nextValue = normalizeSheetCellText(cell.textContent || "");
          cell.textContent = nextValue;
          moveCaretToEnd(cell);
          handlePreviewCellEdit(rowIndex, columnIndex, nextValue, cellRef);
        });
        cell.addEventListener("keydown", (event) => {
          handlePreviewCellKeydown(event, cell, rowIndex, columnIndex, header.length, visibleRowCount);
        });
        cell.addEventListener("blur", () => {
          const nextValue = normalizeSheetCellText(cell.textContent || "");
          if (cell.textContent !== nextValue) {
            cell.textContent = nextValue;
          }
          handlePreviewCellEdit(rowIndex, columnIndex, nextValue, cellRef);
        });
        tr.appendChild(cell);
      }

      tbody.appendChild(tr);
    }

    table.appendChild(thead);
    table.appendChild(tbody);
    els.samplePreviewTableWrap.appendChild(table);
    updateSamplePreviewSelection("A1", header[0] || "");
    highlightSelectedPreviewCell("A1");
  } catch (error) {
    state.samplePreviewRows = [];
    els.samplePreviewMeta.textContent = "Preview unavailable";
    updateSamplePreviewSelection("A1", "");
    els.samplePreviewTableWrap.className = "sheet-grid empty-state";
    els.samplePreviewTableWrap.textContent = `Could not render sample preview: ${error.message}`;
  }
}

function renderResultSummary(summary, csvText) {
  const el = els.resultSummary;
  el.classList.remove("summary-ok", "summary-warn");
  if (!csvText || !csvText.trim()) {
    el.textContent = "No results yet. Run an extraction to see output here.";
    return;
  }
  const rows = summary?.rows ?? 0;
  const needsReview = summary?.sources_manual_review ?? 0;
  const verified = summary?.sources_verified ?? 0;
  let message = `Extracted ${rows} row${rows === 1 ? "" : "s"}`;
  if (verified) {
    message += ` from ${verified} source${verified === 1 ? "" : "s"}`;
  }
  message += ".";
  if (needsReview > 0) {
    message += ` ${needsReview} source${needsReview === 1 ? "" : "s"} need review.`;
    el.classList.add("summary-warn");
  } else {
    el.classList.add("summary-ok");
  }
  el.textContent = message;
}

function renderReadOnlyGrid(target, csvText, metaEl) {
  const trimmed = (csvText || "").trim();
  const setMeta = (text) => { if (metaEl) metaEl.textContent = text; };
  if (!trimmed) {
    target.className = "sheet-grid empty-state";
    target.textContent = "No extracted rows yet.";
    setMeta("No result yet.");
    return;
  }
  let rows;
  try {
    rows = parseCsv(trimmed);
  } catch (error) {
    target.className = "sheet-grid empty-state";
    target.textContent = `Could not render result: ${error.message}`;
    setMeta("Preview unavailable");
    return;
  }
  if (!rows.length) {
    target.className = "sheet-grid empty-state";
    target.textContent = "No extracted rows yet.";
    setMeta("No result yet.");
    return;
  }
  setMeta(`${Math.max(rows.length - 1, 0)} rows • ${rows[0].length} columns`);

  const header = rows[0];
  const bodyRows = rows.slice(1);
  target.className = "sheet-grid";
  target.innerHTML = "";

  const table = document.createElement("table");
  table.className = "sheet-table";
  table.style.width = `${totalTableWidth(header)}px`;

  const columnGroup = document.createElement("colgroup");
  const rowNumberCol = document.createElement("col");
  rowNumberCol.className = "sheet-row-index-col";
  rowNumberCol.style.width = `${ROW_INDEX_COL_WIDTH}px`;
  columnGroup.appendChild(rowNumberCol);
  for (const columnName of header) {
    const col = document.createElement("col");
    col.style.width = `${estimateColumnWidth(columnName)}px`;
    columnGroup.appendChild(col);
  }
  table.appendChild(columnGroup);

  const thead = document.createElement("thead");
  const headerRow = document.createElement("tr");
  const corner = document.createElement("th");
  corner.className = "sheet-corner";
  headerRow.appendChild(corner);
  for (const columnName of header) {
    const th = document.createElement("th");
    th.className = "sheet-column-letter";
    th.textContent = columnName;
    headerRow.appendChild(th);
  }
  thead.appendChild(headerRow);

  const tbody = document.createElement("tbody");
  bodyRows.forEach((row, rowIndex) => {
    const tr = document.createElement("tr");
    const rowHeader = document.createElement("th");
    rowHeader.className = "sheet-row-number";
    rowHeader.textContent = String(rowIndex + 1);
    tr.appendChild(rowHeader);
    for (let columnIndex = 0; columnIndex < header.length; columnIndex += 1) {
      const cell = document.createElement("td");
      cell.className = "sheet-cell";
      cell.textContent = row[columnIndex] || "";
      tr.appendChild(cell);
    }
    tbody.appendChild(tr);
  });

  table.appendChild(thead);
  table.appendChild(tbody);
  target.appendChild(table);
}

function downloadFinalCsv() {
  if (!state.cleanCsv.trim()) {
    setStatus("No extracted CSV is available to download yet.");
    return;
  }
  const blob = new Blob([state.cleanCsv], { type: "text/csv;charset=utf-8" });
  const objectUrl = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = objectUrl;
  anchor.download = state.cleanCsvName || "extracted.csv";
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(objectUrl);
  setStatus(`Downloaded ${state.cleanCsvName}.`);
}

function resolveSamplePreviewTitle() {
  const samplePath = els.samplePathBox.textContent.trim();
  if (samplePath && samplePath !== "No sample yet.") {
    const pieces = samplePath.split(/[\\/]/);
    const existingFilename = pieces[pieces.length - 1];
    if (existingFilename) {
      return existingFilename;
    }
  }
  return "";
}

function updateSamplePreviewSelection(cellRef, cellValue) {
  state.samplePreviewSelection = { ref: cellRef, value: cellValue };
  els.samplePreviewCellRef.textContent = cellRef;
  els.samplePreviewFormula.textContent = cellValue || "";
}

function handlePreviewCellEdit(rowIndex, columnIndex, nextValue, cellRef) {
  ensurePreviewRowShape(rowIndex, columnIndex);
  state.samplePreviewRows[rowIndex][columnIndex] = nextValue;
  syncSampleEditorFromPreviewRows();
  updateSamplePreviewSelection(cellRef, nextValue);
}

function ensurePreviewRowShape(rowIndex, columnIndex) {
  const headerWidth = Math.max(state.samplePreviewRows[0]?.length || 0, columnIndex + 1);

  while (state.samplePreviewRows.length <= rowIndex) {
    state.samplePreviewRows.push(Array.from({ length: headerWidth }, () => ""));
  }

  for (const row of state.samplePreviewRows) {
    while (row.length < headerWidth) {
      row.push("");
    }
  }
}

function syncSampleEditorFromPreviewRows() {
  const serialized = serializeCsv(state.samplePreviewRows);
  els.sampleCsvEditor.value = serialized;
  const dataRowCount = Math.max(state.samplePreviewRows.length - 1, 0);
  const columnCount = state.samplePreviewRows[0]?.length || 0;
  els.samplePreviewMeta.textContent = `${dataRowCount} rows • ${columnCount} columns`;
  updateButtonAvailability();
}

function highlightSelectedPreviewCell(cellRef) {
  const selectedCells = els.samplePreviewTableWrap.querySelectorAll(".sheet-cell.is-selected");
  for (const cell of selectedCells) {
    cell.classList.remove("is-selected");
  }

  const activeCell = els.samplePreviewTableWrap.querySelector(`[data-cell-ref="${cellRef}"]`);
  if (activeCell) {
    activeCell.classList.add("is-selected");
  }
}

function serializeCsv(rows) {
  return rows.map((row) => row.map(escapeCsvCell).join(",")).join("\n");
}

function escapeCsvCell(value) {
  const text = String(value ?? "");
  if (/[",\n\r]/.test(text)) {
    return `"${text.replaceAll("\"", "\"\"")}"`;
  }
  return text;
}

function normalizeSheetCellText(value) {
  // Flatten to a single line and collapse runs of whitespace so long cell content
  // (source-extracted descriptions carry stray tabs/newlines/double-spaces) renders
  // cleanly instead of showing gaps inside the cell.
  return String(value ?? "")
    .replace(/\s+/g, " ")
    .trim();
}

function moveCaretToEnd(element) {
  const selection = window.getSelection();
  if (!selection) {
    return;
  }
  const range = document.createRange();
  range.selectNodeContents(element);
  range.collapse(false);
  selection.removeAllRanges();
  selection.addRange(range);
}

// Caret position within a contentEditable cell — used so Left/Right only jump cells
// when the caret is already at the text edge, otherwise they move within the text.
function caretAtCellStart() {
  const selection = window.getSelection();
  if (!selection || selection.rangeCount === 0) {
    return true;
  }
  const range = selection.getRangeAt(0);
  return range.collapsed && range.startOffset === 0;
}

function caretAtCellEnd(element) {
  const selection = window.getSelection();
  if (!selection || selection.rangeCount === 0) {
    return true;
  }
  const range = selection.getRangeAt(0);
  const textLength = (element.textContent || "").length;
  return range.collapsed && range.startOffset === textLength;
}

function focusPreviewCell(rowIndex, columnIndex) {
  const cellRef = `${spreadsheetColumnLabel(columnIndex)}${rowIndex + 1}`;
  const target = els.samplePreviewTableWrap.querySelector(`[data-cell-ref="${cellRef}"]`);
  if (!target) {
    return;
  }
  target.focus();
  moveCaretToEnd(target);
  updateSamplePreviewSelection(cellRef, target.textContent || "");
  highlightSelectedPreviewCell(cellRef);
  target.scrollIntoView({ block: "nearest", inline: "nearest" });
}

function handlePreviewCellKeydown(event, cell, rowIndex, columnIndex, columnCount, rowCount) {
  const lastRow = rowCount - 1;
  const lastColumn = columnCount - 1;

  if (event.key === "Enter") {
    event.preventDefault();
    focusPreviewCell(Math.min(rowIndex + 1, lastRow), columnIndex);
    return;
  }
  if (event.key === "Tab") {
    event.preventDefault();
    if (event.shiftKey) {
      if (columnIndex > 0) {
        focusPreviewCell(rowIndex, columnIndex - 1);
      } else if (rowIndex > 0) {
        focusPreviewCell(rowIndex - 1, lastColumn);
      }
    } else if (columnIndex < lastColumn) {
      focusPreviewCell(rowIndex, columnIndex + 1);
    } else if (rowIndex < lastRow) {
      focusPreviewCell(rowIndex + 1, 0);
    }
    return;
  }
  if (event.key === "ArrowUp") {
    event.preventDefault();
    focusPreviewCell(Math.max(rowIndex - 1, 0), columnIndex);
    return;
  }
  if (event.key === "ArrowDown") {
    event.preventDefault();
    focusPreviewCell(Math.min(rowIndex + 1, lastRow), columnIndex);
    return;
  }
  if (event.key === "ArrowLeft" && caretAtCellStart() && columnIndex > 0) {
    event.preventDefault();
    focusPreviewCell(rowIndex, columnIndex - 1);
    return;
  }
  if (event.key === "ArrowRight" && caretAtCellEnd(cell) && columnIndex < lastColumn) {
    event.preventDefault();
    focusPreviewCell(rowIndex, columnIndex + 1);
  }
}

function spreadsheetColumnLabel(index) {
  let label = "";
  let value = index;
  do {
    label = String.fromCharCode(65 + (value % 26)) + label;
    value = Math.floor(value / 26) - 1;
  } while (value >= 0);
  return label;
}

const ROW_INDEX_COL_WIDTH = 58;

function estimateColumnWidth(columnName) {
  const normalized = String(columnName || "").trim().toLowerCase();
  // Description stays wide; every other column is compact and fixed.
  if (normalized === "description") {
    return 560;
  }
  if (normalized === "source") {
    return 150;
  }
  if (normalized === "subject" || normalized === "domain" || normalized === "topic") {
    return 130;
  }
  if (normalized === "display standard code" || normalized === "standard code") {
    return 110;
  }
  if (normalized === "grade_level" || normalized === "display_grade" || normalized === "grade_number") {
    return 90;
  }
  if (normalized === "l3" || normalized === "l4" || normalized === "l5") {
    return 150;
  }
  if (normalized === "czi_standard_code") {
    return 110;
  }
  return 110;
}

// table-layout:fixed only honors <col> widths reliably when the table itself has an
// explicit width. Without it, browsers fall back to auto layout and columns grow with
// content. So we pin the table width to the exact sum of its column widths.
function totalTableWidth(header) {
  let total = ROW_INDEX_COL_WIDTH;
  for (const columnName of header) {
    total += estimateColumnWidth(columnName);
  }
  return total;
}

function parseCsv(csvText) {
  const rows = [];
  let row = [];
  let cell = "";
  let insideQuotes = false;

  for (let index = 0; index < csvText.length; index += 1) {
    const char = csvText[index];
    const nextChar = csvText[index + 1];

    if (char === "\"") {
      if (insideQuotes && nextChar === "\"") {
        cell += "\"";
        index += 1;
      } else {
        insideQuotes = !insideQuotes;
      }
      continue;
    }

    if (char === "," && !insideQuotes) {
      row.push(cell);
      cell = "";
      continue;
    }

    if ((char === "\n" || char === "\r") && !insideQuotes) {
      if (char === "\r" && nextChar === "\n") {
        index += 1;
      }
      row.push(cell);
      rows.push(row);
      row = [];
      cell = "";
      continue;
    }

    cell += char;
  }

  row.push(cell);
  rows.push(row);
  return rows.filter((currentRow) => currentRow.length > 1 || currentRow[0] !== "");
}

function initializeManagedButtons() {
  for (const button of [
    els.generateDraftButton,
    els.runExtractionButton,
    els.approveDraftButton,
    els.downloadFinalButton,
  ]) {
    if (!button) {
      continue;
    }
    enhanceButtonMarkup(button);
    managedButtons.push(button);
  }
}

function enhanceButtonMarkup(button) {
  if (button.dataset.enhanced === "true") {
    return;
  }

  const label = button.textContent.trim();
  button.dataset.defaultLabel = label;
  button.textContent = "";

  const labelSpan = document.createElement("span");
  labelSpan.className = "button-label";
  labelSpan.textContent = label;

  const spinner = document.createElement("span");
  spinner.className = "button-spinner";
  spinner.setAttribute("aria-hidden", "true");

  button.append(labelSpan, spinner);
  button.dataset.enhanced = "true";
}

async function runButtonAction(button, action) {
  const startedAt = performance.now();
  state.isBusy = true;
  setButtonState(button, "loading");
  updateButtonAvailability();
  try {
    const result = await action();
    await ensureMinimumLoadingTime(startedAt);
    setButtonState(button, "success");
    return result;
  } catch (error) {
    await ensureMinimumLoadingTime(startedAt);
    setButtonState(button, "idle");
    throw error;
  } finally {
    state.isBusy = false;
    updateButtonAvailability();
  }
}

function setButtonState(button, stateName) {
  clearButtonStateTimer(button);
  button.classList.toggle("is-loading", stateName === "loading");
  button.classList.toggle("is-success", stateName === "success");
  button.dataset.state = stateName;

  if (stateName === "success") {
    const timerId = window.setTimeout(() => {
      if (button.dataset.state === "success") {
        setButtonState(button, "idle");
        updateButtonAvailability();
      }
    }, BUTTON_SUCCESS_MS);
    button.dataset.stateTimerId = String(timerId);
  }
}

function clearButtonStateTimer(button) {
  const timerId = Number(button.dataset.stateTimerId || "");
  if (!Number.isNaN(timerId) && timerId > 0) {
    window.clearTimeout(timerId);
  }
  delete button.dataset.stateTimerId;
}

async function ensureMinimumLoadingTime(startedAt) {
  const elapsed = performance.now() - startedAt;
  const remaining = MIN_BUTTON_LOADING_MS - elapsed;
  if (remaining > 0) {
    await new Promise((resolve) => window.setTimeout(resolve, remaining));
  }
}

function updateButtonAvailability() {
  const hasSample = Boolean(els.sampleCsvEditor.value.trim());

  els.generateDraftButton.disabled = state.isBusy;
  els.runExtractionButton.disabled = state.isBusy;
  els.approveDraftButton.hidden = !hasSample || state.showFinal;
  els.approveDraftButton.disabled = state.isBusy;
  els.downloadFinalButton.disabled = state.isBusy || !state.cleanCsv.trim();
}

function renderDocumentList() {}

function switchTab(which) {
  const isReview = which === "review";
  els.reviewTab.hidden = !isReview;
  els.extractionTab.hidden = isReview;
  els.tabReview.classList.toggle("active", isReview);
  els.tabExtraction.classList.toggle("active", !isReview);
  els.tabReview.setAttribute("aria-selected", String(isReview));
  els.tabExtraction.setAttribute("aria-selected", String(!isReview));
  els.formPanelTitle.textContent = isReview ? "Review CSV" : "New Extraction";
}

els.tabExtraction.addEventListener("click", () => switchTab("extraction"));
els.tabReview.addEventListener("click", () => {
  switchTab("review");
  if (!reviewState.batchesLoaded) loadReviewBatches();
});

function setReviewMode(mode) {
  reviewState.mode = mode;
  const isBatch = mode === "batch";
  els.reviewBatchMode.hidden = !isBatch;
  els.reviewExternalMode.hidden = isBatch;
  els.reviewModeBatch.classList.toggle("active", isBatch);
  els.reviewModeExternal.classList.toggle("active", !isBatch);
  els.reviewModeBatch.setAttribute("aria-selected", String(isBatch));
  els.reviewModeExternal.setAttribute("aria-selected", String(!isBatch));
  els.reviewSummary.textContent = isBatch ? "" : "";
  els.reviewFindings.innerHTML = "";
  els.reviewFixRow.hidden = true;
}

els.reviewModeBatch.addEventListener("click", () => setReviewMode("batch"));
els.reviewModeExternal.addEventListener("click", () => setReviewMode("external"));

async function loadReviewBatches() {
  try {
    const payload = await getJson("/api/review-batches");
    const batches = payload.batches || [];
    reviewState.batchesLoaded = true;
    if (!batches.length) {
      els.reviewBatchSelect.innerHTML = '<option value="">No extracted CSVs yet</option>';
      return;
    }
    batches.sort((a, b) => (Number(b.mtime) || 0) - (Number(a.mtime) || 0));
    const labelCounts = new Map();
    const labels = batches.map((b) => {
      const baseLabel = b.subject || b.name;
      const count = labelCounts.get(baseLabel) || 0;
      labelCounts.set(baseLabel, count + 1);
      return {
        value: b.name,
        label: count === 0 ? baseLabel : `${baseLabel} (${count})`,
      };
    });
    els.reviewBatchSelect.innerHTML =
      '<option value="">Select a subject…</option>' +
      labels
        .map((item) => `<option value="${escapeAttr(item.value)}">${escapeHtml(item.label)}</option>`)
        .join("");
  } catch (error) {
    els.reviewBatchSelect.innerHTML = '<option value="">Failed to load</option>';
  }
}

els.reviewBatchSelect.addEventListener("change", async () => {
  const batchName = els.reviewBatchSelect.value;
  if (!batchName) {
    return;
  }
  try {
    await runButtonAction(els.reviewBatchSelect, async () => {
      await loadBatch(batchName);
    });
  } catch (error) {
    els.reviewSummary.textContent = error.message;
  }
});

els.reviewCsvFile.addEventListener("change", async (event) => {
  reviewState.csvFile = event.target.files[0] || null;
  reviewState.csvText = reviewState.csvFile ? await reviewState.csvFile.text() : "";
  showReviewCsvInPreview(reviewState.csvText, reviewState.csvFile ? reviewState.csvFile.name : "");
});

function showReviewCsvInPreview(csvText, title) {
  els.previewHeading.textContent = "CSV under review";
  els.previewPill.textContent = "Review";
  els.samplePreviewTitle.textContent = title || "Uploaded CSV";
  renderReadOnlyGrid(els.samplePreviewTableWrap, csvText || "", els.samplePreviewMeta);
  els.resultSummary.hidden = true;
}

els.reviewSourceFile.addEventListener("change", (event) => {
  reviewState.sourceFile = event.target.files[0] || null;
});

els.reviewCsvButton.addEventListener("click", async () => {
  const instructions = els.reviewInstructions.value.trim();
  let request;
  if (reviewState.mode === "batch") {
    const name = els.reviewBatchSelect.value;
    if (!name) {
      els.reviewSummary.textContent = "Select an extracted subject to review.";
      return;
    }
    request = async () => postJson("/api/review-batch", { name, review_instructions: instructions });
  } else {
    if (!reviewState.csvFile) {
      els.reviewSummary.textContent = "Upload a CSV to review.";
      return;
    }
    const sourceUrl = els.reviewSourceUrl.value.trim();
    if (!reviewState.sourceFile && !sourceUrl) {
      els.reviewSummary.textContent = "Provide a source document (upload) or a source URL.";
      return;
    }
    request = async () =>
      postJson("/api/review-csv", {
        csv_file: (await encodeFiles([reviewState.csvFile]))[0],
        source_files: reviewState.sourceFile ? await encodeFiles([reviewState.sourceFile]) : [],
        source_urls: sourceUrl,
        review_instructions: instructions,
      });
  }
  els.reviewSummary.textContent = "";
  els.reviewFindings.innerHTML = "";
  try {
    await runButtonAction(els.reviewCsvButton, async () => {
      const response = await request();
      renderReviewResult(response.review);
    });
  } catch (error) {
    els.reviewSummary.textContent = error.message;
  }
});

function renderReviewResult(review) {
  if (!review) {
    els.reviewSummary.textContent = "No review result returned.";
    return;
  }
  const { rows_audited = 0, issue_count = 0, findings = [], source = "" } = review;
  els.reviewSummary.textContent =
    issue_count === 0
      ? `Reviewed ${rows_audited} row(s) against ${source} — no issues found.`
      : `Reviewed ${rows_audited} row(s) against ${source} — ${issue_count} issue(s) found.`;

  reviewState.findings = findings;
  // Offer the fix step when there are findings OR the user typed suggestions.
  const canFix = findings.length > 0 || els.reviewSuggestions.value.trim().length > 0;
  els.reviewFixRow.hidden = !canFix;
  els.downloadFixedButton.hidden = true;
  els.reviewFixSummary.hidden = true;
  reviewState.correctedCsv = "";

  const autoItems = findings
    .map((f, i) => {
      const rn = f.row_number != null ? f.row_number : "";
      const rowLabel = f.row_number != null ? `Row ${f.row_number}` : "Row";
      const col = f.column_name ? ` · ${escapeHtml(f.column_name)}` : "";
      const type = f.issue_type ? `<span class="finding-type">${escapeHtml(f.issue_type)}</span>` : "";
      const msg = escapeHtml(f.issue_message || "");
      const suppressed = Boolean(f.suppressed);
      // Suppressed = your review instruction declared it acceptable. Shown dimmed,
      // unchecked by default (so it isn't fixed), but you can re-check to override.
      const suppressedNote = suppressed
        ? `<span class="finding-suppressed">suppressed by your instruction: ${escapeHtml(f.suppressed_reason || "")}</span>`
        : "";
      return `<li class="finding-item${suppressed ? " suppressed" : ""}">
        <label class="finding-check">
          <input type="checkbox" class="finding-toggle" data-row="${rn}" data-issue="${escapeAttr(f.issue_message || f.issue_type || "issue")}" ${suppressed ? "" : "checked"}>
          <span class="finding-body">
            <span class="finding-head">${escapeHtml(rowLabel)}${col} ${type}</span>
            <span class="finding-msg">${msg}</span>
            ${suppressedNote}
          </span>
        </label>
      </li>`;
    })
    .join("");

  els.reviewFindings.innerHTML = `
    ${findings.length ? `<p class="inline-note">Uncheck any finding the reviewer got wrong (external CSVs may follow valid conventions it doesn't know).</p><ul class="finding-list">${autoItems}</ul>` : `<p class="inline-note">No reviewer findings. You can still add your own below.</p>`}
    <div class="manual-finding">
      <span class="inline-note">Add a finding the reviewer missed:</span>
      <div class="manual-finding-row">
        <input id="manualFindingRow" type="number" min="2" placeholder="Row #" class="manual-row-input">
        <input id="manualFindingIssue" type="text" placeholder="What's wrong / what to fix" class="manual-issue-input">
        <button id="addManualFindingButton" class="ghost" type="button">Add</button>
      </div>
      <ul id="manualFindingList" class="finding-list"></ul>
    </div>`;

  wireManualFindingControls();
}

function escapeAttr(value) {
  return String(value).replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function wireManualFindingControls() {
  const addButton = document.getElementById("addManualFindingButton");
  const rowInput = document.getElementById("manualFindingRow");
  const issueInput = document.getElementById("manualFindingIssue");
  const list = document.getElementById("manualFindingList");
  if (!addButton) return;
  addButton.addEventListener("click", () => {
    const rn = parseInt(rowInput.value, 10);
    const issue = issueInput.value.trim();
    if (!rn || rn < 2 || !issue) {
      els.reviewFixSummary.hidden = false;
      els.reviewFixSummary.textContent = "Enter a row number (2+) and an issue to add.";
      return;
    }
    const li = document.createElement("li");
    li.className = "finding-item manual";
    li.innerHTML = `<label class="finding-check">
      <input type="checkbox" class="finding-toggle" data-row="${rn}" data-issue="${escapeAttr(issue)}" checked>
      <span class="finding-body"><span class="finding-head">Row ${rn} <span class="finding-type">manual</span></span><span class="finding-msg">${escapeHtml(issue)}</span></span>
    </label>`;
    list.appendChild(li);
    rowInput.value = "";
    issueInput.value = "";
  });
}

function collectApprovedFindings() {
  const approved = [];
  document.querySelectorAll("#reviewFindings .finding-toggle:checked").forEach((cb) => {
    const rn = parseInt(cb.getAttribute("data-row"), 10);
    const issue = cb.getAttribute("data-issue") || "";
    if (rn && issue) approved.push({ row_number: rn, issue });
  });
  return approved;
}

async function buildReviewSourcePayload(extra = {}) {
  return {
    csv_file: (await encodeFiles([reviewState.csvFile]))[0],
    source_files: reviewState.sourceFile ? await encodeFiles([reviewState.sourceFile]) : [],
    source_urls: els.reviewSourceUrl.value.trim(),
    suggestions: els.reviewSuggestions.value.trim(),
    ...extra,
  };
}

els.approveFixButton.addEventListener("click", async () => {
  const approved_findings = collectApprovedFindings();
  const suggestions = els.reviewSuggestions.value.trim();
  let request;
  if (reviewState.mode === "batch") {
    const name = els.reviewBatchSelect.value;
    if (!name) {
      els.reviewFixSummary.hidden = false;
      els.reviewFixSummary.textContent = "Select an extracted subject first.";
      return;
    }
    request = async () => postJson("/api/fix-reviewed-batch", { name, approved_findings, suggestions });
  } else {
    if (!reviewState.csvFile) {
      els.reviewFixSummary.hidden = false;
      els.reviewFixSummary.textContent = "Upload a CSV first.";
      return;
    }
    request = async () =>
      postJson("/api/fix-reviewed-csv", await buildReviewSourcePayload({ approved_findings }));
  }
  els.reviewFixSummary.hidden = false;
  els.reviewFixSummary.textContent = "";
  try {
    await runButtonAction(els.approveFixButton, async () => {
      const response = await request();
      const fix = response.fix || {};
      reviewState.correctedCsv = fix.corrected_csv || "";
      reviewState.correctedName = fix.corrected_name || "reviewed.fixed.csv";
      els.reviewFixSummary.textContent = fix.message || `Fixed ${fix.fixed_count || 0} row(s).`;
      els.downloadFixedButton.hidden = !reviewState.correctedCsv;
      if (reviewState.correctedCsv) {
        showReviewCsvInPreview(reviewState.correctedCsv, reviewState.correctedName);
      }
    });
  } catch (error) {
    els.reviewFixSummary.textContent = error.message;
  }
});

els.downloadFixedButton.addEventListener("click", () => {
  if (!reviewState.correctedCsv) return;
  downloadTextAsFile(reviewState.correctedCsv, reviewState.correctedName || "reviewed.fixed.csv");
});

async function encodeFiles(files) {
  const uploads = [];
  for (const file of files) {
    uploads.push({
      name: file.name,
      content_base64: await fileToBase64(file),
    });
  }
  return uploads;
}

async function fileToBase64(file) {
  const arrayBuffer = await file.arrayBuffer();
  const bytes = new Uint8Array(arrayBuffer);
  let binary = "";
  const chunkSize = 0x8000;
  for (let index = 0; index < bytes.length; index += chunkSize) {
    const chunk = bytes.subarray(index, index + chunkSize);
    binary += String.fromCharCode(...chunk);
  }
  return btoa(binary);
}

// API base prefix. Empty for the local server (routes at /api/...); set to
// "/_vibes/main/py" by the hosted (Vibe) index.html so the same code targets the
// proxied Python backend. window.__API_BASE__ is injected by the server that serves
// this file.
const API_BASE = (typeof window !== "undefined" && window.__API_BASE__) || "";

function apiUrl(path) {
  return `${API_BASE}${path}`;
}

async function getJson(url) {
  const response = await fetch(apiUrl(url), {
    headers: {
      Accept: "application/json",
    },
  });
  return parseResponse(response);
}

async function postJson(url, payload) {
  const response = await fetch(apiUrl(url), {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Accept: "application/json",
    },
    body: JSON.stringify(payload),
  });
  return parseResponse(response);
}

async function parseResponse(response) {
  const payload = await response.json();
  if (!response.ok) {
    throw new Error(payload.error || `Request failed with status ${response.status}.`);
  }
  return payload;
}

function stringifyPretty(value, fallback) {
  if (!value || (typeof value === "object" && !Object.keys(value).length)) {
    return fallback;
  }
  if (typeof value === "string") {
    return value;
  }
  return JSON.stringify(value, null, 2);
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

initializeManagedButtons();
renderResultSummary(null, "");
renderPreview();
updateButtonAvailability();
refreshWorkspace();
seedBlankSampleTemplate();
