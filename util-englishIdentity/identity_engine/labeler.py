"""LLM labeler — names dirty clusters with canonical patterns.

After embedding-based clustering creates new expressions (or membership shifts),
each expression has `label_dirty=1`. This stage picks up dirty rows, samples
representative member sentences, asks the LLM to write a canonical pattern,
and stores it via set_expression_label (which also recomputes kr_hash).

Uses the same CLI/API backend as translation.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

from .config import AppConfig, build_label_prompt
from .database import Database


class Labeler:
    def __init__(self, config: AppConfig, db: Database, lang: str = "kr"):
        self._cfg = config
        self._db = db
        self._lang = lang
        if config.translation_backend == "cli":
            self._verify_cli()
            self._api_client = None
        else:
            from google import genai
            self._api_client = genai.Client(api_key=config.gemini_api_key)

    def _verify_cli(self) -> None:
        if not shutil.which(self._cfg.gemini_cli_path):
            raise RuntimeError(
                f"gemini CLI not found on PATH (looked for: {self._cfg.gemini_cli_path!r})."
            )

    def label_dirty(
        self, min_member_count: int = 1, on_progress=None,
    ) -> tuple[int, int]:
        """Label all dirty pending expressions. Returns (ok, err)."""
        rows = [
            r for r in self._db.get_dirty_expressions(self._lang, statuses=["pending"])
            if int(r["member_count"]) >= min_member_count
        ]
        if not rows:
            return 0, 0

        batch_size = max(1, self._cfg.mega_batch_size // 4)  # smaller per call (richer payloads)
        parallel = max(1, self._cfg.mega_parallel)
        chunks = [rows[i : i + batch_size] for i in range(0, len(rows), batch_size)]
        total_ok = total_err = 0

        def process(chunk):
            return self._label_chunk(chunk, on_progress)

        if parallel == 1:
            for idx, chunk in enumerate(chunks):
                start = time.monotonic()
                ok, err = process(chunk)
                elapsed = time.monotonic() - start
                if on_progress:
                    on_progress(
                        None,
                        info=f"[label {idx+1}/{len(chunks)}] {len(chunk)} clusters in {elapsed:.1f}s",
                    )
                total_ok += ok
                total_err += err
            return total_ok, total_err

        with ThreadPoolExecutor(max_workers=parallel) as pool:
            futures = {pool.submit(process, c): len(c) for c in chunks}
            completed = 0
            for future in as_completed(futures):
                ok, err = future.result()
                completed += 1
                if on_progress:
                    on_progress(None, info=f"[label batch {completed}/{len(chunks)}]")
                total_ok += ok
                total_err += err
        return total_ok, total_err

    def _label_chunk(
        self, rows: list[sqlite3.Row], on_progress,
    ) -> tuple[int, int]:
        sample_n = max(1, int(self._cfg.label_sample_size))
        items = []
        for r in rows:
            instances = self._db.get_instances_for_expression(r["id"])
            samples = [i["kr_text"] for i in instances[:sample_n]]
            if not samples:
                samples = [r["kr_expr"]]
            items.append({"id": int(r["id"]), "samples": samples})

        prompt = build_label_prompt(items, lang=self._lang)
        try:
            text = self._invoke(prompt)
            results = self._parse_json_array(text)
        except Exception as exc:
            if on_progress:
                on_progress(None, error=f"label: {exc}")
            return 0, len(rows)

        by_id = {int(r["id"]): r for r in results if "id" in r}
        ok = err = 0
        for r in rows:
            res = by_id.get(int(r["id"]))
            canonical = (res or {}).get("canonical", "").strip() if res else ""
            gloss = (res or {}).get("gloss", "").strip() if res else ""
            if canonical:
                self._db.set_expression_label(r["id"], canonical)
                if gloss:
                    self._db.set_expression_gloss(r["id"], gloss)
                ok += 1
            else:
                err += 1
        return ok, err

    # ── backend invocation (shared shape) ────────────────────────────────────

    def _invoke(self, prompt: str) -> str:
        if self._api_client is not None:
            return self._invoke_api(prompt)
        return self._invoke_cli(prompt)

    def _invoke_api(self, prompt: str) -> str:
        from google.genai import types
        config = types.GenerateContentConfig(temperature=0.2, max_output_tokens=4096)
        response = self._api_client.models.generate_content(
            model=self._cfg.gemini_model, contents=prompt, config=config,
        )
        return response.text or ""

    def _invoke_cli(self, prompt: str) -> str:
        cmd = [
            self._cfg.gemini_cli_path,
            "-m", self._cfg.gemini_model,
            "--approval-mode", "plan",
            "--skip-trust",
            "-o", "text",
            "-p", prompt,
        ]
        env = {k: v for k, v in os.environ.items() if k != "GEMINI_API_KEY"}
        sandbox = tempfile.mkdtemp(prefix="identity_label_")
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True,
                timeout=self._cfg.cli_timeout, check=False,
                env=env, cwd=sandbox,
            )
        finally:
            try:
                Path(sandbox).rmdir()
            except OSError:
                pass
        if proc.returncode != 0:
            raise RuntimeError(f"gemini CLI exit {proc.returncode}: {proc.stderr.strip()[:500]}")
        return proc.stdout

    @staticmethod
    def _parse_json_array(text: str) -> list[dict]:
        start = text.find("[")
        end = text.rfind("]")
        if start < 0 or end <= start:
            raise ValueError(f"no JSON array in label output: {text[:200]!r}")
        return json.loads(text[start : end + 1])
