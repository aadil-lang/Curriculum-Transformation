const state = {
  currentBatch: null,
  documents: [],
  sampleCsvFile: null,
};

const els = {
  batchName: document.getElementById("batchName"),
  instructions: document.getElementById("instructions"),
  documentFiles: document.getElementById("documentFiles"),
  sampleCsvFile: document.getElementById("sampleCsvFile"),
  documentList: document.getElementById("documentList"),
  sampleCsvLabel: document.getElementById("sampleCsvLabel"),
  sampleCsvEditor: document.getElementById("sampleCsvEditor"),
  statusBox: document.getElementById("statusBox"),
  schemaBox: document.getElementById("schemaBox"),
  outputBox: document.getElementById("outputBox"),
  manualReviewBox: document.getElementById("manualReviewBox"),
  batchList: document.getElementById("batchList"),
  batchBadge: document.getElementById("batchBadge"),
  generateDraftButton: document.getElementById("generateDraftButton"),
  runExtractionButton: document.getElementById("runExtractionButton"),
  approveRunButton: document.getElementById("approveRunButton"),
  refreshButton: document.getElementById("refreshButton"),
  reloadBatchesButton: document.getElementById("reloadBatchesButton"),
};

els.documentFiles.addEventListener("change", async (event) => {
  state.documents = Array.from(event.target.files || []);
  renderDocumentList();
});

els.sampleCsvFile.addEventListener("change", async (event) => {
  const file = Array.from(event.target.files || [])[0] || null;
  state.sampleCsvFile = file;
  els.sampleCsvLabel.textContent = file ? file.name : "No sample CSV selected yet.";
  if (file) {
    els.sampleCsvEditor.value = await file.text();
    els.approveRunButton.disabled = false;
  }
});

els.generateDraftButton.addEventListener("click", async () => {
  const batchName = normalizedBatchName();
  const instructions = els.instructions.value.trim();
  if (!batchName) {
    setStatus("Enter a batch name first.");
    return;
  }
  if (!instructions) {
    setStatus("Instructions are required to draft a sample CSV.");
    return;
  }

  setStatus("Generating a draft sample CSV from your instructions...");
  toggleButtons(true);
  try {
    const response = await postJson("/api/draft-sample", {
      name: batchName,
      instructions,
      document_files: await encodeFiles(state.documents),
    });
    hydrateBatch(response.batch);
    setStatus(JSON.stringify(response.result, null, 2));
    await loadBatchList();
  } catch (error) {
    setStatus(error.message);
  } finally {
    toggleButtons(false);
  }
});

els.runExtractionButton.addEventListener("click", async () => {
  await runExtractionFromEditorOrUpload();
});

els.approveRunButton.addEventListener("click", async () => {
  await runExtractionFromEditorOrUpload();
});

els.refreshButton.addEventListener("click", async () => {
  const batchName = normalizedBatchName();
  if (!batchName) {
    setStatus("Enter or load a batch name to refresh it.");
    return;
  }
  await loadBatch(batchName);
});

els.reloadBatchesButton.addEventListener("click", async () => {
  await loadBatchList();
});

async function runExtractionFromEditorOrUpload() {
  const batchName = normalizedBatchName();
  if (!batchName) {
    setStatus("Enter a batch name first.");
    return;
  }

  const sampleCsvContent = els.sampleCsvEditor.value.trim();
  if (!sampleCsvContent) {
    setStatus("Approve or upload a sample CSV before extraction.");
    return;
  }

  setStatus("Running extraction with the approved sample CSV...");
  toggleButtons(true);
  try {
    const response = await postJson("/api/run-extraction", {
      name: batchName,
      instructions: els.instructions.value.trim(),
      sample_csv_name: state.sampleCsvFile ? state.sampleCsvFile.name : "approved_sample.csv",
      sample_csv_content: sampleCsvContent,
      document_files: await encodeFiles(state.documents),
    });
    hydrateBatch(response.batch);
    setStatus(JSON.stringify(response.result, null, 2));
    await loadBatchList();
  } catch (error) {
    setStatus(error.message);
  } finally {
    toggleButtons(false);
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
  els.batchName.value = batch.name;
  els.instructions.value = batch.instructions || els.instructions.value;
  els.sampleCsvEditor.value = batch.approved_sample_csv || batch.sample_template_csv || "";
  els.schemaBox.textContent = stringifyPretty(batch.schema_config, "No schema yet.");
  els.outputBox.textContent = batch.final_csv || "No output yet.";
  els.manualReviewBox.textContent = stringifyPretty(
    {
      manual_review: batch.manual_review,
      processing_state: batch.processing_state,
      monitor_status: batch.monitor_status,
    },
    "No manual review entries.",
  );
  els.batchBadge.textContent = batch.status || "Idle";
  els.approveRunButton.disabled = !els.sampleCsvEditor.value.trim();
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
        <p>Status: ${escapeHtml(batch.status)}</p>
        <p>Documents: ${batch.document_count}</p>
        <p>Updated: ${escapeHtml(batch.updated_at_utc)}</p>
        <button type="button" data-batch="${escapeHtml(batch.name)}">Open Batch</button>
      `;
      card.querySelector("button").addEventListener("click", async () => {
        await loadBatch(batch.name);
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

function normalizedBatchName() {
  return els.batchName.value.trim();
}

function setStatus(message) {
  els.statusBox.textContent = message;
}

function toggleButtons(isBusy) {
  for (const button of [
    els.generateDraftButton,
    els.runExtractionButton,
    els.approveRunButton,
    els.refreshButton,
    els.reloadBatchesButton,
  ]) {
    button.disabled = isBusy;
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

renderDocumentList();
loadBatchList();
