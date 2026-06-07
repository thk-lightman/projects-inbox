"""Optional transform blocks. Each is a pure rows->rows function — snap any
number of them between read_sheet and write_sheet.
"""

from __future__ import annotations

from collections import OrderedDict


def group_merge(
    rows: list[dict], group_col: str, text_col: str, sep: str = " --- "
) -> list[dict]:
    """Group rows by group_col, merging non-empty text_col values with sep."""

    groups: "OrderedDict[str, list[str]]" = OrderedDict()
    for row in rows:
        key = row.get(group_col, "")
        text = str(row.get(text_col, "")).strip()
        if not text:
            continue
        groups.setdefault(key, []).append(text)
    return [
        {group_col: key, "Merged_Text": sep.join(texts), "Row_Count": len(texts)}
        for key, texts in groups.items()
    ]


def llm_map(rows: list[dict], fn, text_key: str = "Merged_Text") -> list[dict]:
    """Apply fn(text)->dict to each row's text_key, merging the result into the row.

    fn is provider-agnostic — pass any callable (Gemini, OpenAI, regex, ...).
    A failing row is captured in a 'Status' field instead of aborting the batch.
    """

    out: list[dict] = []
    for row in rows:
        merged = {**row}
        try:
            merged.update(fn(str(row.get(text_key, ""))))
            merged.setdefault("Status", "OK")
        except Exception as exc:  # one bad row must not kill the batch
            merged["Status"] = f"ERROR: {exc}"
        out.append(merged)
    return out
