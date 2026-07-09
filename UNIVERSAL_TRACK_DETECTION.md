# Universal Progression Matrix Detection

This document explains how the Data Transformation Agent universally detects and handles curriculum option-track structures (like CST/TS/S) without requiring manual configuration for each document family.

## Problem Solved

Different curricula use different differentiation vocabularies:
- **Quebec PFEQ**: CST / TS / S (three academic streams)
- **Other provinces**: Academic / Applied, Standard / Enriched, etc.
- **Some curricula**: No differentiation at all

The old system required env var configuration (`PROGRESSION_MATRIX_OPTION_TRACKS=CST,TS,S`) for each document family. The new system auto-discovers tracks from the document and maps them to the sample CSV structure automatically.

---

## Three-Layer Universal System

### Layer 1: Auto-discovery from Document

**File**: `parsers/pdf_parser.py` → `_discover_option_tracks()`

The parser scans each page for repeated short uppercase labels (2-8 chars) that appear at least 6 times in a vertical column pattern (typically on the right side of the page, x > 400).

**How it works**:
```python
def _discover_option_tracks(spans: list[dict[str, Any]]) -> tuple[str, ...]:
    """
    Finds: CST (appears 30× on page 41)
          TS  (appears 30× on page 41)
          S   (appears 30× on page 41)
    
    Returns: ('TS', 'CST')  # Sorted by frequency descending
    """
```

**Fallback**: If auto-discovery yields nothing, uses `settings.progression_matrix_option_tracks` (default: `CST,TS,S` from `.env`).

**Result**: Parser hints blocks now show:
```
## Parsed progression-matrix placement hints
This page uses a TS / CST option-track matrix (auto-discovered).
- Item 6: Compares financial situations | applies_to=CST | not_for=TS
```

---

### Layer 2: Sample CSV Track Structure Analysis

**File**: `extractor.py` → `PreExtractionUnderstanding.sample_csv_track_structure`

The pre-extraction analysis pass inspects the **Display standard code** column in the approved sample CSV to determine:
1. Which standards include track suffixes
2. What the track segment format is
3. When tracks are omitted vs. required

**Example from Canada sample CSV**:
```csv
Display standard code
E6.MAT.AR-UR.1.a          ← No track (Elementary)
S1.MAT.AR-UR.1.a          ← No track (Secondary 1-3)
S5.MAT.CST.AR-UR.11.h     ← Has CST track (Secondary 4-5)
S5.MAT.TS.AR-UR.11.g      ← Has TS track
S5.MAT.S.AR-UR.11.g       ← Has S track
```

**Analysis fills**:
```json
{
  "sample_csv_track_structure": [
    "Display codes for Elementary and Secondary 1-3 omit track segment",
    "Display codes for Secondary 4-5 include .CST., .TS., or .S. after subject code",
    "Format: grade.subject.TRACK.domain.code when track differentiation exists"
  ]
}
```

---

### Layer 3: Document-to-Sample Track Mapping

**File**: `extractor.py` → `PreExtractionUnderstanding.document_to_sample_track_mapping`

The model maps **document track labels** (discovered from the page or stated in legends) to **sample CSV track segments**:

```json
{
  "document_to_sample_track_mapping": {
    "CST": "CST",
    "TS": "TS",
    "S": "S"
  }
}
```

For a different curriculum:
```json
{
  "document_to_sample_track_mapping": {
    "Academic": "ACD",
    "Applied": "APL"
  }
}
```

**During extraction**, the model:
1. Reads parsed placement hints: `Item 6: applies_to=CST | not_for=TS`
2. Consults the mapping: `CST → CST`
3. Constructs display code: `S5.MAT.CST.FM.6` (not `S5.MAT.TS.FM.6` or `S5.MAT.S.FM.6`)

---

## How It All Works Together

### Example: Quebec PFEQ Math PDF

1. **Parser auto-discovers**: `CST`, `TS`, `S` from page 41
2. **Parser emits hints**:
   ```
   - Item 6: Compares financial situations | applies_to=CST | not_for=TS,S
   ```

3. **Pre-extraction analysis reads sample CSV**, finds:
   - Some codes have no track: `S1.MAT.AR-UR.1.a`
   - Some have track: `S5.MAT.CST.AR-UR.11.h`

4. **Model fills**:
   ```json
   {
     "sample_csv_track_structure": [
       "S1-S3 standards omit track; S4-S5 include .CST., .TS., or .S."
     ],
     "document_to_sample_track_mapping": {
       "CST": "CST", "TS": "TS", "S": "S"
     },
     "row_scope_rules": [
       "Item 6 belongs to CST only; emit S5.MAT.CST.FM.6 row only"
     ]
   }
   ```

5. **Extraction pass**:
   - Reads hint: `Item 6: applies_to=CST`
   - Maps: `CST → CST`
   - Emits: **one row** with `Display standard code = S5.MAT.CST.FM.6`
   - Skips TS and S rows (per `not_for=TS,S`)

---

## Benefits

### ✅ Zero configuration for similar document families
Quebec PFEQ docs with CST/TS/S work automatically — no env var needed.

### ✅ Adapts to new curricula
A different province with Academic/Applied tracks will:
1. Auto-discover `Academic`, `Applied` from the page
2. Map them to sample CSV track codes
3. Produce correct display codes

### ✅ Graceful degradation
If auto-discovery fails:
- Falls back to configured tracks (`PROGRESSION_MATRIX_OPTION_TRACKS`)
- If no tracks found, still uses model's legend-reading (always worked)

### ✅ No false positives
The system won't add track suffixes to display codes unless:
1. Sample CSV shows track structure exists
2. Document has placement hints or legends confirming tracks
3. Mapping is established in pre-extraction understanding

---

## Configuration (Optional)

The system is **zero-config by default**, but you can still override:

### Manual track vocabulary (when auto-discovery isn't sufficient)
```bash
# .env
PROGRESSION_MATRIX_OPTION_TRACKS=Academic,Applied,Enriched
```

This serves as a **fallback** when auto-discovery doesn't find anything, or as a **hint** to prioritize certain labels.

---

## Testing Different Curricula

### Example 1: Quebec PFEQ (tested)
```bash
# No configuration needed
python main.py chat-batch --sample-csv canada_sample.csv quebec_math.pdf
```
**Result**: Item 6 → `S5.MAT.CST.FM.6` (CST only, not TS/S)

### Example 2: Ontario Curriculum (hypothetical)
Suppose Ontario uses "Academic" and "Applied" tracks, and the sample CSV has:
```csv
Display standard code
G9.MAT.Academic.A1.1
G9.MAT.Applied.A1.1
```

**What happens**:
1. Parser auto-discovers: `Academic`, `Applied`
2. Analysis finds sample uses `.Academic.` and `.Applied.` segments
3. Mapping: `{"Academic": "Academic", "Applied": "Applied"}`
4. Extraction: Placement hints guide which rows get which track suffix

**Zero configuration required.**

---

## Summary

The universal system makes track detection **document-driven + sample-driven** instead of configuration-driven:

| **Component** | **What It Does** | **Source of Truth** |
|---------------|------------------|---------------------|
| Auto-discovery | Finds track vocabulary | Document pages |
| Track structure analysis | Determines display code format | Sample CSV |
| Track mapping | Links document → sample | Pre-extraction understanding |
| Scoping enforcement | One row per marked track | Parser hints + model |

**No env vars required for standard curricula. Adapts automatically to new track vocabularies.**
