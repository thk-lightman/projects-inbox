"""Sync Manager — pushes translated expressions (with EN instance examples) to
AnkiConnect + Google Sheets. (Audio handled by Anki AwesomeTTS.)
"""
import html
import sqlite3
from typing import Optional

import gspread
import requests
from google.oauth2.service_account import Credentials

from .config import AppConfig
from .database import Database, STATUS_SYNCED, STATUS_TRANSLATED


_ANKI_NOTE_FIELDS = ("Front", "Gloss", "Examples", "KRExpression", "Sources")
_SHEETS_HEADERS = ["KR_Expr", "EN_Expr", "Gloss", "Freq", "Num_Instances", "Sources"]

_SCOPES = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive",
]


class SyncManager:
    def __init__(self, config: AppConfig, db: Database):
        self._cfg = config
        self._db = db
        self._sheets_client: Optional[gspread.Client] = None

    # ── Anki ──────────────────────────────────────────────────────────────────

    def _anki_request(self, action: str, **params) -> dict:
        payload = {"action": action, "version": 6, "params": params}
        resp = requests.post(self._cfg.ankiconnect_url, json=payload, timeout=10)
        resp.raise_for_status()
        return resp.json()

    def _ensure_anki_model(self) -> None:
        existing = self._anki_request("modelNames").get("result", [])
        if self._cfg.anki_model in existing:
            return
        self._anki_request(
            "createModel",
            modelName=self._cfg.anki_model,
            inOrderFields=list(_ANKI_NOTE_FIELDS),
            cardTemplates=[
                {
                    "Name": "Expression Card",
                    "Front": "<div class='expr'>{{Front}}</div>",
                    "Back": (
                        "<div class='expr'>{{Front}}</div>"
                        "<hr id=answer>"
                        "<div class='gloss'>{{Gloss}}</div>"
                        "<ul class='examples'>{{Examples}}</ul>"
                        "<div class='kr'>{{KRExpression}}</div>"
                        "<div class='sources'><small>{{Sources}}</small></div>"
                    ),
                }
            ],
        )

    def _ensure_anki_deck(self) -> None:
        self._anki_request("createDeck", deck=self._cfg.anki_deck)

    def _build_note(self, expr: sqlite3.Row, instances: list[sqlite3.Row]) -> dict:
        example_items = []
        sources = set()
        for inst in instances:
            en = (inst["en_text"] or "").strip()
            kr = (inst["kr_text"] or "").strip()
            if not en:
                continue
            example_items.append(
                f"<li>{html.escape(en)}"
                f"<br><small>{html.escape(kr)}</small></li>"
            )
            sources.add(inst["source_file"])
        examples_html = "".join(example_items) if example_items else "<li><em>(no examples)</em></li>"
        sources_str = ", ".join(sorted(sources))
        return {
            "deckName": self._cfg.anki_deck,
            "modelName": self._cfg.anki_model,
            "fields": {
                "Front": expr["en_expr"] or "",
                "Gloss": expr["gloss"] or "",
                "Examples": examples_html,
                "KRExpression": expr["kr_expr"] or "",
                "Sources": sources_str,
            },
            "options": {"allowDuplicate": False},
            "tags": ["identity-engine", "expression", f"freq-{expr['freq']}"],
        }

    def push_expressions_to_anki(
        self, expressions: list[sqlite3.Row], on_progress=None
    ) -> tuple[int, int]:
        self._ensure_anki_deck()
        self._ensure_anki_model()

        notes = []
        expr_order = []
        for expr in expressions:
            instances = self._db.get_instances_for_expression(expr["id"])
            notes.append(self._build_note(expr, instances))
            expr_order.append(expr)

        if not notes:
            return 0, 0

        result = self._anki_request("addNotes", notes=notes)
        result_ids = result.get("result") or []

        added = error = 0
        for expr, nid in zip(expr_order, result_ids):
            if nid is not None:
                self._db.set_expression_status(expr["id"], STATUS_SYNCED)
                added += 1
            else:
                error += 1
            if on_progress:
                on_progress(expr["id"])
        return added, error

    # ── Google Sheets ─────────────────────────────────────────────────────────

    def _get_sheet(self) -> gspread.Worksheet:
        if self._sheets_client is None:
            creds = Credentials.from_service_account_file(
                self._cfg.gcp_credentials, scopes=_SCOPES
            )
            self._sheets_client = gspread.authorize(creds)
        spreadsheet = self._sheets_client.open_by_key(self._cfg.sheets_id)
        try:
            ws = spreadsheet.worksheet(self._cfg.sheets_worksheet)
        except gspread.WorksheetNotFound:
            ws = spreadsheet.add_worksheet(self._cfg.sheets_worksheet, rows=1000, cols=10)
            ws.append_row(_SHEETS_HEADERS)
        return ws

    def push_expressions_to_sheets(self, expressions: list[sqlite3.Row]) -> int:
        if not self._cfg.sheets_id:
            return 0
        ws = self._get_sheet()
        new_rows = []
        for expr in expressions:
            instances = self._db.get_instances_for_expression(expr["id"])
            sources = sorted({i["source_file"] for i in instances})
            new_rows.append([
                expr["kr_expr"],
                expr["en_expr"] or "",
                expr["gloss"] or "",
                expr["freq"],
                len(instances),
                ", ".join(sources),
            ])
        if new_rows:
            ws.append_rows(new_rows, value_input_option="RAW")
        return len(new_rows)

    # ── unified sync ─────────────────────────────────────────────────────────

    def sync_pending(self, on_progress=None) -> dict:
        expressions = self._db.get_expressions_by_status(STATUS_TRANSLATED)
        if not expressions:
            return {"anki_added": 0, "anki_error": 0, "sheets_added": 0}

        anki_added, anki_err = self.push_expressions_to_anki(
            expressions, on_progress=on_progress
        )
        sheets_added = self.push_expressions_to_sheets(expressions)

        return {
            "anki_added": anki_added,
            "anki_error": anki_err,
            "sheets_added": sheets_added,
        }
