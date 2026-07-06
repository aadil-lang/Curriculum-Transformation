const state = {
  currentBatch: null,
  documents: [],
  sampleCsvFile: null,
  samplePreviewSelection: { ref: "A1", value: "" },
  samplePreviewRows: [],
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
  workspaceSchema: document.getElementById("workspaceSchema"),
  workspaceBatches: document.getElementById("workspaceBatches"),
  workspaceInputs: document.getElementById("workspaceInputs"),
  workspaceSheets: document.getElementById("workspaceSheets"),
  batchName: document.getElementById("batchName"),
  instructions: document.getElementById("instructions"),
  documentFiles: document.getElementById("documentFiles"),
  sampleCsvFile: document.getElementById("sampleCsvFile"),
  documentList: document.getElementById("documentList"),
  sampleCsvLabel: document.getElementById("sampleCsvLabel"),
  sampleCsvEditor: document.getElementById("sampleCsvEditor"),
  samplePreviewTitle: document.getElementById("samplePreviewTitle"),
  samplePreviewMeta: document.getElementById("samplePreviewMeta"),
  samplePreviewCellRef: document.getElementById("samplePreviewCellRef"),
  samplePreviewFormula: document.getElementById("samplePreviewFormula"),
  samplePreviewTableWrap: document.getElementById("samplePreviewTableWrap"),
  statusBox: document.getElementById("statusBox"),
  schemaBox: document.getElementById("schemaBox"),
  schemaPathBox: document.getElementById("schemaPathBox"),
  samplePathBox: document.getElementById("samplePathBox"),
  outputBox: document.getElementById("outputBox"),
  actionBox: document.getElementById("actionBox"),
  batchMetaBox: document.getElementById("batchMetaBox"),
  auditReportBox: document.getElementById("auditReportBox"),
  manualReviewBox: document.getElementById("manualReviewBox"),
  batchList: document.getElementById("batchList"),
  batchBadge: document.getElementById("batchBadge"),
  generateDraftButton: document.getElementById("generateDraftButton"),
  runExtractionButton: document.getElementById("runExtractionButton"),
  downloadSampleButton: document.getElementById("downloadSampleButton"),
  approveRunButton: document.getElementById("approveRunButton"),
  auditBatchButton: document.getElementById("auditBatchButton"),
  syncFinalButton: document.getElementById("syncFinalButton"),
  syncSampleButton: document.getElementById("syncSampleButton"),
  refreshButton: document.getElementById("refreshButton"),
  reloadBatchesButton: document.getElementById("reloadBatchesButton"),
};

els.documentFiles.addEventListener("change", async (event) => {
  state.documents = Array.from(event.target.files || []);
  renderDocumentList();
});

els.sampleCsvEditor.addEventListener("input", () => {
  renderSamplePreview(els.sampleCsvEditor.value);
  updateButtonAvailability();
});

els.sampleCsvFile.addEventListener("change", async (event) => {
  const file = Array.from(event.target.files || [])[0] || null;
  state.sampleCsvFile = file;
  els.sampleCsvLabel.textContent = file ? file.name : "No sample CSV selected yet.";
  if (file) {
    els.sampleCsvEditor.value = await file.text();
  }
  updateButtonAvailability();
});

els.generateDraftButton.addEventListener("click", async () => {
  const batchName = ensureBatchName();
  const instructions = els.instructions.value.trim();
  if (!instructions) {
    setStatus("Instructions are required to draft a sample CSV.");
    return;
  }

  setStatus("Generating a draft sample CSV from your instructions...");
  try {
    await runButtonAction(els.generateDraftButton, async () => {
      const response = await postJson("/api/draft-sample", {
        name: batchName,
        instructions,
        document_files: await encodeFiles(state.documents),
      });
      hydrateBatch(response.batch);
      setStatus(
        response.result?.queued
          ? `${response.result.message} Job id: ${response.result.job_id}`
          : JSON.stringify(response.result, null, 2),
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

els.downloadSampleButton.addEventListener("click", async () => {
  try {
    await runButtonAction(els.downloadSampleButton, async () => {
      downloadCurrentSampleCsv();
    });
  } catch (error) {
    setStatus(error.message);
  }
});

els.approveRunButton.addEventListener("click", async () => {
  await runExtractionFromEditorOrUpload(els.approveRunButton);
});

els.auditBatchButton.addEventListener("click", async () => {
  const batchName = normalizedBatchName();
  if (!batchName) {
    setStatus("Load a batch before running audit.");
    return;
  }
  setActionStatus("Running batch audit...");
  try {
    await runButtonAction(els.auditBatchButton, async () => {
      const response = await postJson("/api/audit-batch", { name: batchName });
      hydrateBatch(response.batch);
      setActionStatus(JSON.stringify(response.audit, null, 2));
      await loadWorkspaceSummary();
    });
  } catch (error) {
    setActionStatus(error.message);
  }
});

els.syncFinalButton.addEventListener("click", async () => {
  await runSync(els.syncFinalButton, false);
});

els.syncSampleButton.addEventListener("click", async () => {
  await runSync(els.syncSampleButton, true);
});

els.refreshButton.addEventListener("click", async () => {
  const batchName = normalizedBatchName();
  if (!batchName) {
    setStatus("Enter or load a batch name to refresh it.");
    return;
  }
  try {
    await runButtonAction(els.refreshButton, async () => {
      await loadBatch(batchName);
    });
  } catch (error) {
    setStatus(error.message);
  }
});

els.reloadBatchesButton.addEventListener("click", async () => {
  try {
    await runButtonAction(els.reloadBatchesButton, async () => {
      await refreshWorkspace();
    });
  } catch (error) {
    setStatus(error.message);
  }
});

async function runExtractionFromEditorOrUpload(triggerButton) {
  const batchName = ensureBatchName();
  if (!batchName) {
    setStatus("Add instructions or a document so the app can create a batch name.");
    return;
  }

  const sampleCsvContent = els.sampleCsvEditor.value.trim();
  if (!sampleCsvContent) {
    setStatus("Approve or upload a sample CSV before extraction.");
    return;
  }

  setStatus("Running extraction with the approved sample CSV...");
  try {
    await runButtonAction(triggerButton, async () => {
      const response = await postJson("/api/run-extraction", {
        name: batchName,
        instructions: els.instructions.value.trim(),
        sample_csv_name: state.sampleCsvFile ? state.sampleCsvFile.name : "approved_sample.csv",
        sample_csv_content: sampleCsvContent,
        document_files: await encodeFiles(state.documents),
      });
      hydrateBatch(response.batch);
      setStatus(
        response.result?.queued
          ? `${response.result.message} Job id: ${response.result.job_id}`
          : JSON.stringify(response.result, null, 2),
      );
      await refreshWorkspace();
    });
  } catch (error) {
    setStatus(error.message);
  }
}

async function runSync(triggerButton, sample) {
  const batchName = normalizedBatchName();
  if (!batchName) {
    setStatus("Load a batch before running sheet sync.");
    return;
  }
  setActionStatus(sample ? "Syncing approved sample to Sheets..." : "Syncing final CSV to Sheets...");
  try {
    await runButtonAction(triggerButton, async () => {
      const response = await postJson("/api/sync-batch", { name: batchName, sample });
      hydrateBatch(response.batch);
      setActionStatus(JSON.stringify(response.sync, null, 2));
      await loadWorkspaceSummary();
    });
  } catch (error) {
    setActionStatus(error.message);
  }
}

function renderDocumentList() {
  els.documentList.innerHTML = "";
  if (!state.documents.length) {
    const placeholder = document.createElement("div");
    placeholder.className = "inline-note";
    placeholder.textContent = "No documents selected yet.";
    els.documentList.appendChild(placeholder);
    return;
  }

  for (const file of state.documents) {
    const chip = document.createElement("span");
    chip.className = "chip";
    chip.textContent = file.name;
    els.documentList.appendChild(chip);
  }
}

function hydrateBatch(batch) {
  state.currentBatch = batch.name;
  state.batchCapabilities.hasFinalCsv = Boolean(batch.final_csv_path);
  state.batchCapabilities.hasSampleArtifact = Boolean(batch.approved_sample_csv_path || batch.sample_template_path);
  els.batchName.value = batch.name;
  els.instructions.value = batch.instructions || els.instructions.value;
  els.sampleCsvEditor.value = batch.approved_sample_csv || batch.sample_template_csv || "";
  els.schemaBox.textContent = stringifyPretty(batch.schema_config, "No schema yet.");
  els.schemaPathBox.textContent = batch.schema_config_path || "No schema yet.";
  els.samplePathBox.textContent = batch.approved_sample_csv_path || batch.sample_template_path || "No sample yet.";
  els.outputBox.textContent = batch.final_csv || "No output yet.";
  els.actionBox.textContent = stringifyPretty(
    {
      codex_job_status: batch.codex_job_status,
      csv_finalization_status: batch.csv_finalization_status,
      google_sheets_sync_status: batch.google_sheets_sync_status,
    },
    "No batch action run yet.",
  );
  els.batchMetaBox.textContent = stringifyPretty(
    {
      status: batch.status,
      batch_root: batch.batch_root,
      document_files: batch.document_files,
      codex_job_status: batch.codex_job_status,
      schema_config_path: batch.schema_config_path,
      approved_sample_csv_path: batch.approved_sample_csv_path,
      final_csv_path: batch.final_csv_path,
      latest_audit_report_path: batch.latest_audit_report_path,
    },
    "No batch loaded.",
  );
  els.auditReportBox.textContent = stringifyPretty(batch.latest_audit_report, "No audit report yet.");
  els.manualReviewBox.textContent = stringifyPretty(
    {
      codex_job_status: batch.codex_job_status,
      manual_review: batch.manual_review,
      processing_state: batch.processing_state,
      monitor_status: batch.monitor_status,
    },
    "No manual review entries.",
  );
  els.batchBadge.textContent = batch.status || "Idle";
  renderSamplePreview(els.sampleCsvEditor.value);
  updateBatchPolling(batch);
  updateButtonAvailability();
}

async function refreshWorkspace() {
  await Promise.all([loadBatchList(), loadWorkspaceSummary()]);
}

async function loadWorkspaceSummary() {
  try {
    const workspace = await getJson("/api/workspace");
    els.workspaceSchema.textContent = workspace.default_schema_name || "Unknown";
    els.workspaceBatches.textContent = String(workspace.batch_count ?? 0);
    els.workspaceInputs.textContent = String(workspace.input_document_count ?? 0);
    if (!workspace.google_sheets_sync_enabled) {
      els.workspaceSheets.textContent = "Disabled";
    } else if (workspace.google_sheets_spreadsheet_id_present) {
      els.workspaceSheets.textContent = "Configured";
    } else {
      els.workspaceSheets.textContent = "Needs Setup";
    }
  } catch (error) {
    els.workspaceSchema.textContent = "Unavailable";
    els.workspaceBatches.textContent = "-";
    els.workspaceInputs.textContent = "-";
    els.workspaceSheets.textContent = "Unavailable";
  }
}

async function loadBatchList() {
  try {
    const payload = await getJson("/api/status");
    els.batchList.innerHTML = "";
    const batches = payload.batches || [];
    if (!batches.length) {
      els.batchList.textContent = "No batches yet.";
      return;
    }

    for (const batch of batches) {
      const card = document.createElement("div");
      card.className = "batch-item";
      card.innerHTML = `
        <h3>${escapeHtml(batch.name)}</h3>
        <p><span class="mini-badge">${escapeHtml(batch.status)}</span></p>
        <p>Documents: ${batch.document_count}</p>
        <p>Schema: ${batch.has_schema ? "Yes" : "No"}</p>
        <p>Final CSV: ${batch.has_final_csv ? "Yes" : "No"}</p>
        <p>Updated: ${escapeHtml(batch.updated_at_utc)}</p>
        <button type="button" data-batch="${escapeHtml(batch.name)}">Open Batch</button>
      `;
      const openButton = card.querySelector("button");
      enhanceButtonMarkup(openButton);
      openButton.disabled = state.isBusy;
      openButton.addEventListener("click", async () => {
        try {
          await runButtonAction(openButton, async () => {
            await loadBatch(batch.name);
          });
        } catch (error) {
          setStatus(error.message);
        }
      });
      els.batchList.appendChild(card);
    }
  } catch (error) {
    els.batchList.textContent = error.message;
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
  return els.batchName.value.trim();
}

function ensureBatchName() {
  const existing = normalizedBatchName();
  if (existing) {
    return existing;
  }

  const derived = deriveBatchName();
  if (!derived) {
    return "";
  }

  els.batchName.value = derived;
  return derived;
}

function deriveBatchName() {
  const instructionText = els.instructions.value.trim();
  const documentName = state.documents[0]?.name || state.sampleCsvFile?.name || "";
  const seed = instructionText || documentName;
  if (!seed) {
    return "";
  }

  const cleaned = seed
    .toLowerCase()
    .replace(/\.[a-z0-9]+$/i, "")
    .replace(/[^a-z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "")
    .slice(0, 48)
    .replace(/^_+|_+$/g, "");

  if (!cleaned) {
    return "";
  }

  return cleaned;
}

function setStatus(message) {
  els.statusBox.textContent = message;
}

function setActionStatus(message) {
  els.actionBox.textContent = message;
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

  const batchName = ensureBatchName();
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

    const columnGroup = document.createElement("colgroup");
    const rowNumberCol = document.createElement("col");
    rowNumberCol.className = "sheet-row-index-col";
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
        const cellValue = sourceRow[columnIndex] || "";
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
          if (event.key === "Enter") {
            event.preventDefault();
            cell.blur();
          }
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

function resolveSamplePreviewTitle() {
  const samplePath = els.samplePathBox.textContent.trim();
  if (samplePath && samplePath !== "No sample yet.") {
    const pieces = samplePath.split(/[\\/]/);
    const existingFilename = pieces[pieces.length - 1];
    if (existingFilename) {
      return existingFilename;
    }
  }

  return resolveSampleDownloadFilename();
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
  return value.replace(/\r/g, "").replace(/\n/g, " ");
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

function spreadsheetColumnLabel(index) {
  let label = "";
  let value = index;
  do {
    label = String.fromCharCode(65 + (value % 26)) + label;
    value = Math.floor(value / 26) - 1;
  } while (value >= 0);
  return label;
}

function estimateColumnWidth(columnName) {
  const normalized = String(columnName || "").trim().toLowerCase();
  if (normalized === "source") {
    return 280;
  }
  if (normalized === "description") {
    return 460;
  }
  if (normalized === "subject" || normalized === "domain" || normalized === "topic") {
    return 240;
  }
  if (normalized === "display standard code" || normalized === "standard code") {
    return 170;
  }
  if (normalized === "grade_level" || normalized === "display_grade" || normalized === "grade_number") {
    return 160;
  }
  if (normalized === "l3" || normalized === "l4" || normalized === "l5") {
    return 180;
  }
  if (normalized === "czi_standard_code") {
    return 180;
  }
  return 190;
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
    els.downloadSampleButton,
    els.approveRunButton,
    els.auditBatchButton,
    els.syncFinalButton,
    els.syncSampleButton,
    els.refreshButton,
    els.reloadBatchesButton,
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
  const hasBatch = Boolean(normalizedBatchName());

  els.generateDraftButton.disabled = state.isBusy;
  els.runExtractionButton.disabled = state.isBusy;
  els.downloadSampleButton.disabled = state.isBusy || !hasSample;
  els.approveRunButton.disabled = state.isBusy || !hasSample;
  els.auditBatchButton.disabled = state.isBusy || !state.batchCapabilities.hasFinalCsv;
  els.syncFinalButton.disabled = state.isBusy || !state.batchCapabilities.hasFinalCsv;
  els.syncSampleButton.disabled = state.isBusy || !state.batchCapabilities.hasSampleArtifact;
  els.refreshButton.disabled = state.isBusy || !hasBatch;
  els.reloadBatchesButton.disabled = state.isBusy;

  for (const openButton of els.batchList.querySelectorAll(".batch-item button")) {
    openButton.disabled = state.isBusy;
  }
}

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

async function getJson(url) {
  const response = await fetch(url, {
    headers: {
      Accept: "application/json",
    },
  });
  return parseResponse(response);
}

async function postJson(url, payload) {
  const response = await fetch(url, {
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
renderDocumentList();
renderSamplePreview("");
updateButtonAvailability();
refreshWorkspace();
