"""Load a question-set workbook into seed_data.json.

Usage (from the backend folder):

    python -m app.seed.import_questions "path/to/AI Maturity - question set.xlsx"

The workbook's first sheet holds one question per row under a header row that starts with
"Q-ID" (columns: Q-ID, Dim., Dimension name, Sub-theme, Question, Lens, Level, Status,
Source / previous ID, Evidence type, How the agent answers it, Answer type, Cadence).

The file becomes the question set: questions it adds are created, changed ones are updated and
questions it no longer lists are removed, together with their results, the next time the API
starts (see ensure_question_set in seed.py). Removed questions are added to the Guide's
"What was removed" list.
"""
import json
import sys
from pathlib import Path

DATA_FILE = Path(__file__).with_name("seed_data.json")
LENS = {"MATURITY": "Maturity", "COMPETITIVE – VENDOR": "Competitive · vendor", "COMPETITIVE – RETAILER": "Competitive · retailer"}
HEADERS = ["Q-ID", "Dim.", "Dimension name", "Sub-theme", "Question", "Lens", "Level", "Status",
           "Source / previous ID", "Evidence type", "How the agent answers it", "Answer type", "Cadence"]


def origin_of(prev: str) -> str:
    """'v1 D1-08' → 'Carried · D1-08'; 'Adopted – Mayank' → 'New · Mayank'; 'C5i v2' → 'New · C5i v2'."""
    p = (prev or "").strip()
    if p.startswith("v1 "):
        return "Carried · " + p[3:]
    if p.startswith("Adopted – ") or p.startswith("Adopted - "):
        return "New · " + p[10:]
    if p == "C5i v2":
        return "New · C5i v2"
    return "Carried · " + p.replace(" (merged)", "")


def _clean(v):
    return v.strip() if isinstance(v, str) else v


def read_workbook(path: str) -> list[dict]:
    from openpyxl import load_workbook

    ws = load_workbook(path, read_only=True, data_only=True).worksheets[0]
    rows = list(ws.iter_rows(values_only=True))
    head = next((i for i, r in enumerate(rows) if r and str(r[0]).strip() == "Q-ID"), None)
    if head is None:
        raise SystemExit("No header row starting with 'Q-ID' was found on the first sheet.")
    cols = [str(c).strip() if c else "" for c in rows[head]]
    missing = [h for h in HEADERS if h not in cols]
    if missing:
        raise SystemExit(f"Missing columns: {', '.join(missing)}")
    ix = {h: cols.index(h) for h in HEADERS}

    out = []
    for r in rows[head + 1:]:
        qid = _clean(r[ix["Q-ID"]]) if r else None
        if not qid:
            continue
        lens_raw = str(_clean(r[ix["Lens"]])).upper().replace("-", "–")
        if lens_raw not in LENS:
            raise SystemExit(f"{qid}: unknown lens '{r[ix['Lens']]}'.")
        level = r[ix["Level"]]
        prev = _clean(r[ix["Source / previous ID"]]) or ""
        out.append(dict(
            qid=qid, dimension=_clean(r[ix["Dim."]]), sub_theme=_clean(r[ix["Sub-theme"]]), text=_clean(r[ix["Question"]]),
            lens=LENS[lens_raw], level=int(level) if isinstance(level, (int, float)) or (isinstance(level, str) and level.strip().isdigit()) else None,
            change=_clean(r[ix["Status"]]), prev_id=prev or None, evidence_type=_clean(r[ix["Evidence type"]]),
            how=_clean(r[ix["How the agent answers it"]]), answer_type=_clean(r[ix["Answer type"]]),
            cadence=_clean(r[ix["Cadence"]]), in_run=True, origin=origin_of(prev),
        ))
    ids = [q["qid"] for q in out]
    dupes = sorted({x for x in ids if ids.count(x) > 1})
    if dupes:
        raise SystemExit(f"Duplicate Q-IDs: {', '.join(dupes)}")
    return out


def main(path: str) -> None:
    data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    new = read_workbook(path)
    old = {q["qid"]: q for q in data["questions"]}
    keep = {q["qid"] for q in new}
    gone = [q for qid, q in old.items() if qid not in keep]

    removed = data["guide"].setdefault("removed", [])
    listed = {r["old_id"] for r in removed}
    for q in gone:
        old_id = (q.get("prev_id") or q["qid"]).replace("v1 ", "")
        if old_id not in listed:
            removed.append(dict(old_id=old_id, old_dim=q["dimension"], now_in=q["dimension"], question=q["text"],
                                reason="Not in the final question set"))

    added = [q["qid"] for q in new if q["qid"] not in old]
    changed = [q["qid"] for q in new if q["qid"] in old and {k: v for k, v in old[q["qid"]].items()} != q]
    data["questions"] = new
    DATA_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    lenses = {}
    for q in new:
        lenses[q["lens"]] = lenses.get(q["lens"], 0) + 1
    print(f"{len(new)} questions · " + " · ".join(f"{k} {v}" for k, v in lenses.items()))
    print(f"added {len(added)}{(': ' + ', '.join(added)) if added else ''}")
    print(f"changed {len(changed)}{(': ' + ', '.join(changed[:20]) + (' …' if len(changed) > 20 else '')) if changed else ''}")
    print(f"removed {len(gone)}{(': ' + ', '.join(q['qid'] for q in gone)) if gone else ''}")
    print("Restart the API to apply it to the database.")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    main(sys.argv[1])