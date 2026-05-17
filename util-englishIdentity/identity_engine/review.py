"""Review gate — TSV export/import for human curation of expressions.

Export writes a TSV of expressions awaiting review (status='curated' or 'merged').
The user edits in any spreadsheet/editor:
    - Edit the `expr` column to fix wording (hash gets recomputed on import)
    - Set `keep` to 0 (or 'n', 'no', 'false') to drop a row
    - Optionally merge by setting `merge_into_id` to another row's id
After editing, import applies the changes and advances kept rows to status='locked'.
Only locked rows are picked up by the translate stage.
"""
from pathlib import Path
from typing import Optional

from .database import (
    Database,
    STATUS_LOCKED,
    STATUS_MERGED,
)

_TSV_HEADER = ["id", "lang", "freq", "keep", "merge_into_id", "expr", "instance_preview"]
_TRUTHY = {"1", "y", "yes", "true", "t"}
_FALSY = {"0", "n", "no", "false", "f"}


def export_for_review(
    db: Database,
    output_path: Path,
    lang: Optional[str] = None,
    statuses: Optional[list[str]] = None,
    min_freq: int = 1,
    preview_limit: int = 2,
) -> int:
    """Write curated/merged expressions to TSV for human review."""
    statuses = statuses or ["curated", STATUS_MERGED, "pending"]
    rows = db.get_expressions_filtered(lang=lang, statuses=statuses, min_freq=min_freq)
    written = 0
    with output_path.open("w", encoding="utf-8") as f:
        f.write("\t".join(_TSV_HEADER) + "\n")
        for r in rows:
            instances = db.get_instances_for_expression(r["id"])
            preview = " | ".join(_sanitize(i["kr_text"]) for i in instances[:preview_limit])
            f.write(
                "\t".join([
                    str(r["id"]),
                    r["lang"],
                    str(r["freq"]),
                    "1",
                    "",
                    _sanitize(r["kr_expr"]),
                    preview,
                ]) + "\n"
            )
            written += 1
    return written


def import_reviewed(db: Database, input_path: Path) -> dict:
    """Apply review edits from TSV. Idempotent (safe to re-run).

    Returns counts: {locked, deleted, merged, updated, skipped}.
    """
    locked = deleted = merged = updated = skipped = 0
    with input_path.open("r", encoding="utf-8") as f:
        header = f.readline().rstrip("\n").split("\t")
        cols = {name: idx for idx, name in enumerate(header)}
        if "id" not in cols or "expr" not in cols:
            raise ValueError("TSV missing required columns id and expr")
        for line in f:
            line = line.rstrip("\n")
            if not line:
                continue
            parts = line.split("\t")
            try:
                row_id = int(parts[cols["id"]])
            except (ValueError, IndexError):
                skipped += 1
                continue

            keep_raw = (parts[cols["keep"]] if "keep" in cols and cols["keep"] < len(parts) else "1").strip().lower()
            merge_raw = (parts[cols["merge_into_id"]] if "merge_into_id" in cols and cols["merge_into_id"] < len(parts) else "").strip()
            new_expr = parts[cols["expr"]] if cols["expr"] < len(parts) else ""

            if keep_raw in _FALSY:
                db.delete_expression(row_id)
                deleted += 1
                continue

            if merge_raw:
                try:
                    canonical_id = int(merge_raw)
                except ValueError:
                    skipped += 1
                    continue
                if canonical_id == row_id:
                    skipped += 1
                    continue
                db.merge_expressions(canonical_id, [row_id])
                merged += 1
                continue

            new_expr = new_expr.strip()
            if new_expr:
                db.update_expression_text(row_id, new_expr)
                updated += 1
            db.set_expression_status(row_id, STATUS_LOCKED)
            locked += 1

    return {
        "locked": locked,
        "deleted": deleted,
        "merged": merged,
        "updated": updated,
        "skipped": skipped,
    }


def _sanitize(text: str) -> str:
    """TSV-safe: collapse newlines and tabs."""
    return text.replace("\t", " ").replace("\n", " ").replace("\r", " ").strip()
