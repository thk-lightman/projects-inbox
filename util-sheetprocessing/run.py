"""Thin Lego assembler: read_sheet -> group_merge -> write_sheet.

Common case (group + merge, no LLM):
    python run.py --sa credentials/service_account.json --sheet My_Database \
        --in Input_Data --out Output_Data --group-col Group_ID --text-col Raw_Text

To attach an LLM (or any) step, compose in your own script instead:
    from sheet_io import auth, read_sheet, write_sheet
    from transforms import group_merge, llm_map
    client = auth("credentials/service_account.json")
    rows = read_sheet(client, "My_Database", "Input_Data")
    merged = group_merge(rows, "Group_ID", "Raw_Text")
    out = llm_map(merged, my_classifier_fn)        # my_classifier_fn(text)->dict
    write_sheet(client, "My_Database", "Output_Data", out, mode="overwrite")
"""

from __future__ import annotations

import argparse

from sheet_io import auth, read_sheet, write_sheet
from transforms import group_merge


def main() -> None:
    p = argparse.ArgumentParser(description="Read a sheet, group-merge a column, write a sheet.")
    p.add_argument("--sa", required=True, help="Service account JSON path.")
    p.add_argument("--sheet", required=True, help="Spreadsheet title.")
    p.add_argument("--in", dest="in_tab", required=True, help="Input worksheet tab.")
    p.add_argument("--out", dest="out_tab", required=True, help="Output worksheet tab.")
    p.add_argument("--group-col", required=True)
    p.add_argument("--text-col", required=True)
    p.add_argument("--sep", default=" --- ")
    p.add_argument("--mode", default="overwrite", choices=["overwrite", "append"])
    args = p.parse_args()

    client = auth(args.sa)
    rows = read_sheet(client, args.sheet, args.in_tab)
    merged = group_merge(rows, args.group_col, args.text_col, sep=args.sep)
    written = write_sheet(client, args.sheet, args.out_tab, merged, mode=args.mode)
    print(f"wrote {written} rows to '{args.out_tab}'")


if __name__ == "__main__":
    main()
