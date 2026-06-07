"""Reusable Google Sheets I/O — the stable spine.

Compose: read_sheet -> [optional transforms] -> write_sheet.
Rows are plain list[dict] (header -> cell), so any pure rows->rows block snaps in.
"""

from __future__ import annotations

import gspread


def auth(sa_file: str):
    """Return an authorized gspread client from a service-account JSON file."""

    return gspread.service_account(filename=sa_file)


def read_sheet(client, sheet: str, tab: str) -> list[dict]:
    """Read a worksheet into a list of row dicts."""

    ws = client.open(sheet).worksheet(tab)
    return ws.get_all_records()


def write_sheet(
    client, sheet: str, tab: str, rows: list[dict], mode: str = "overwrite"
) -> int:
    """Write row dicts to a worksheet. Returns rows written.

    mode='overwrite' clears and replaces; mode='append' adds below existing data.
    The tab is created if missing. Columns are taken from the first row's keys.
    """

    if mode not in ("overwrite", "append"):
        raise ValueError(f"mode must be 'overwrite' or 'append': {mode}")
    if not rows:
        return 0

    sh = client.open(sheet)
    header = list(rows[0].keys())
    values = [[str(row.get(col, "")) for col in header] for row in rows]

    try:
        ws = sh.worksheet(tab)
    except gspread.exceptions.WorksheetNotFound:
        ws = sh.add_worksheet(title=tab, rows=len(rows) + 1, cols=len(header))
        ws.update([header], "A1")

    if mode == "overwrite":
        ws.clear()
        ws.update([header] + values, "A1")
    else:
        if not ws.get_all_values():
            ws.update([header], "A1")
        ws.append_rows(values, value_input_option="RAW")
    return len(rows)
