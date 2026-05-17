"""Gemini translation engines — API (google-genai SDK) and CLI (subprocess) backends."""
import json
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Protocol

from google import genai
from google.genai import types
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from .config import (
    AppConfig,
    build_expression_translation_prompt,
    build_mega_batch_prompt,
    build_translation_prompt,
)
from .database import Database, STATUS_PENDING, STATUS_TRANSLATED


class TranslationEngine(Protocol):
    def translate_pending(
        self,
        batch_size: int | None = None,
        on_progress=None,
        rows: list | None = None,
    ) -> tuple[int, int]: ...

    def translate_expressions(
        self,
        rows: list,
        on_progress=None,
    ) -> tuple[int, int]: ...


def _make_retry(max_retries: int):
    return retry(
        reraise=True,
        stop=stop_after_attempt(max_retries),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        retry=retry_if_exception_type(Exception),
    )


class GeminiEngine:
    """API backend — one HTTP call per sentence via google-genai SDK."""

    def __init__(self, config: AppConfig, db: Database):
        self._cfg = config
        self._db = db
        self._client = genai.Client(api_key=config.gemini_api_key)
        self._gen_config = types.GenerateContentConfig(
            system_instruction=config.persona.system_instruction,
            temperature=0.3,
            max_output_tokens=2048,
        )

    def translate_batch(
        self,
        rows: list[sqlite3.Row],
        on_progress=None,
    ) -> tuple[int, int]:
        success = error = 0
        for row in rows:
            try:
                en_text = self._translate_one(row["kr_text"], row["folder_key"])
                self._db.set_translation(row["kr_hash"], en_text)
                success += 1
            except Exception as exc:
                self._db.set_error(row["kr_hash"])
                error += 1
                if on_progress:
                    on_progress(row["kr_hash"], error=str(exc))
                continue
            if on_progress:
                on_progress(row["kr_hash"], en_text=en_text)
        return success, error

    def _translate_one(self, kr_text: str, folder_key: str) -> str:
        prompt = build_translation_prompt(kr_text, folder_key, self._cfg.persona)
        generate = _make_retry(self._cfg.max_retries)(self._client.models.generate_content)
        response = generate(
            model=self._cfg.gemini_model,
            contents=prompt,
            config=self._gen_config,
        )
        return (response.text or "").strip()

    def translate_pending(
        self,
        batch_size: int | None = None,
        on_progress=None,
        rows: list | None = None,
    ) -> tuple[int, int]:
        bs = batch_size or self._cfg.batch_size
        if rows is None:
            rows = self._db.get_sentences_by_status(STATUS_PENDING)
        total_ok = total_err = 0
        for i in range(0, len(rows), bs):
            batch = rows[i : i + bs]
            ok, err = self.translate_batch(batch, on_progress=on_progress)
            total_ok += ok
            total_err += err
            if i + bs < len(rows):
                time.sleep(0.5)
        return total_ok, total_err

    def translate_expressions(self, rows, on_progress=None) -> tuple[int, int]:
        raise NotImplementedError(
            "Expression translation is only implemented for the CLI backend. "
            "Set TRANSLATION_BACKEND=cli in .env."
        )


class GeminiCLIEngine:
    """CLI backend — mega-batch via `gemini` subprocess, OAuth-authenticated.

    Each call translates `mega_batch_size` sentences in one prompt. CLI uses
    Google account OAuth (separate quota from API key).
    """

    _ID_LEN = 12

    def __init__(self, config: AppConfig, db: Database):
        self._cfg = config
        self._db = db
        self._verify_cli()

    def _verify_cli(self) -> None:
        path = shutil.which(self._cfg.gemini_cli_path)
        if not path:
            raise RuntimeError(
                f"gemini CLI not found on PATH (looked for: {self._cfg.gemini_cli_path!r}). "
                "Install official Gemini CLI and run `gemini auth login`, "
                "or set GEMINI_CLI_PATH in .env."
            )

    def translate_pending(
        self,
        batch_size: int | None = None,
        on_progress=None,
        rows: list | None = None,
    ) -> tuple[int, int]:
        mega = batch_size or self._cfg.mega_batch_size
        parallel = max(1, self._cfg.mega_parallel)
        if rows is None:
            rows = self._db.get_sentences_by_status(STATUS_PENDING)

        chunks = [rows[i : i + mega] for i in range(0, len(rows), mega)]
        if not chunks:
            return 0, 0

        total_ok = total_err = 0
        if parallel == 1:
            for idx, chunk in enumerate(chunks):
                start = time.monotonic()
                ok, err = self._translate_mega(chunk, on_progress=on_progress)
                elapsed = time.monotonic() - start
                self._log_timing(idx + 1, len(chunks), len(chunk), elapsed, on_progress)
                total_ok += ok
                total_err += err
            return total_ok, total_err

        completed = 0
        completed_lock = threading.Lock()
        with ThreadPoolExecutor(max_workers=parallel) as pool:
            future_to_size = {
                pool.submit(self._timed_translate, chunk, on_progress): len(chunk)
                for chunk in chunks
            }
            for future in as_completed(future_to_size):
                ok, err, elapsed, size = future.result()
                with completed_lock:
                    completed += 1
                    self._log_timing(completed, len(chunks), size, elapsed, on_progress)
                total_ok += ok
                total_err += err
        return total_ok, total_err

    def _timed_translate(self, chunk, on_progress):
        start = time.monotonic()
        ok, err = self._translate_mega(chunk, on_progress=on_progress)
        return ok, err, time.monotonic() - start, len(chunk)

    @staticmethod
    def _log_timing(idx, total, size, elapsed, on_progress):
        per_sent = elapsed / size if size else 0
        msg = f"[batch {idx}/{total}] {size} sent in {elapsed:.1f}s ({per_sent:.2f}s/sent)"
        if on_progress:
            try:
                on_progress(None, info=msg)
            except TypeError:
                pass

    def _translate_mega(
        self, rows: list[sqlite3.Row], on_progress=None
    ) -> tuple[int, int]:
        items = [
            {
                "id": row["kr_hash"][: self._ID_LEN],
                "kr": row["kr_text"],
                "folder": row["folder_key"],
            }
            for row in rows
        ]
        prompt = build_mega_batch_prompt(items, self._cfg.persona)

        try:
            stdout = self._invoke_cli(prompt)
            results = self._parse_json_array(stdout)
        except Exception as exc:
            for row in rows:
                self._db.set_error(row["kr_hash"])
                if on_progress:
                    on_progress(row["kr_hash"], error=str(exc))
            return 0, len(rows)

        by_id = {item.get("id"): (item.get("en") or "").strip() for item in results}
        ok = err = 0
        for row in rows:
            short = row["kr_hash"][: self._ID_LEN]
            en = by_id.get(short)
            if en:
                self._db.set_translation(row["kr_hash"], en)
                ok += 1
                if on_progress:
                    on_progress(row["kr_hash"], en_text=en)
            else:
                self._db.set_error(row["kr_hash"])
                err += 1
                if on_progress:
                    on_progress(row["kr_hash"], error="missing id in response")
        return ok, err

    def _invoke_cli(self, prompt: str) -> str:
        import os

        cmd = [
            self._cfg.gemini_cli_path,
            "-m",
            self._cfg.gemini_model,
            "--approval-mode",
            "plan",
            "--skip-trust",
            "-o",
            "text",
            "-p",
            prompt,
        ]
        env = {k: v for k, v in os.environ.items() if k != "GEMINI_API_KEY"}
        sandbox = tempfile.mkdtemp(prefix="identity_cli_")
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self._cfg.cli_timeout,
                check=False,
                env=env,
                cwd=sandbox,
            )
        finally:
            try:
                Path(sandbox).rmdir()
            except OSError:
                pass

        if proc.returncode != 0:
            raise RuntimeError(
                f"gemini CLI exit {proc.returncode}: {proc.stderr.strip()[:500]}"
            )
        return proc.stdout

    @staticmethod
    def _parse_json_array(text: str) -> list[dict]:
        start = text.find("[")
        end = text.rfind("]")
        if start < 0 or end <= start:
            raise ValueError(f"no JSON array in CLI output: {text[:200]!r}")
        return json.loads(text[start : end + 1])

    # ── expression translation ───────────────────────────────────────────────

    def translate_expressions(
        self,
        rows: list[sqlite3.Row],
        on_progress=None,
    ) -> tuple[int, int]:
        """Mega-batch translate expressions: kr_expr → en_expr + gloss."""
        if not rows:
            return 0, 0
        mega = self._cfg.mega_batch_size
        parallel = max(1, self._cfg.mega_parallel)
        chunks = [rows[i : i + mega] for i in range(0, len(rows), mega)]
        total_ok = total_err = 0

        if parallel == 1:
            for idx, chunk in enumerate(chunks):
                start = time.monotonic()
                ok, err = self._translate_expr_chunk(chunk, on_progress)
                elapsed = time.monotonic() - start
                self._log_timing(idx + 1, len(chunks), len(chunk), elapsed, on_progress)
                total_ok += ok
                total_err += err
            return total_ok, total_err

        completed = 0
        lock = threading.Lock()
        with ThreadPoolExecutor(max_workers=parallel) as pool:
            future_to_size = {
                pool.submit(self._timed_expr, chunk, on_progress): len(chunk)
                for chunk in chunks
            }
            for future in as_completed(future_to_size):
                ok, err, elapsed, size = future.result()
                with lock:
                    completed += 1
                    self._log_timing(completed, len(chunks), size, elapsed, on_progress)
                total_ok += ok
                total_err += err
        return total_ok, total_err

    def _timed_expr(self, chunk, on_progress):
        start = time.monotonic()
        ok, err = self._translate_expr_chunk(chunk, on_progress)
        return ok, err, time.monotonic() - start, len(chunk)

    def _translate_expr_chunk(
        self, rows: list[sqlite3.Row], on_progress=None
    ) -> tuple[int, int]:
        items = [
            {"id": str(row["id"]), "kr_expr": row["kr_expr"]}
            for row in rows
        ]
        prompt = build_expression_translation_prompt(items, self._cfg.persona)

        try:
            stdout = self._invoke_cli(prompt)
            results = self._parse_json_array(stdout)
        except Exception as exc:
            for row in rows:
                self._db.set_expression_status(row["id"], "error")
                if on_progress:
                    on_progress(None, error=f"expr {row['id']}: {exc}")
            return 0, len(rows)

        by_id = {item.get("id"): item for item in results}
        ok = err = 0
        for row in rows:
            item = by_id.get(str(row["id"]))
            en_expr = (item or {}).get("en_expr", "").strip() if item else ""
            gloss = (item or {}).get("gloss", "").strip() if item else ""
            if en_expr:
                self._db.set_expression_translation(row["id"], en_expr, gloss or None)
                ok += 1
            else:
                self._db.set_expression_status(row["id"], "error")
                err += 1
            if on_progress:
                on_progress(None)
        return ok, err


def make_engine(config: AppConfig, db: Database) -> TranslationEngine:
    if config.translation_backend == "cli":
        return GeminiCLIEngine(config, db)
    return GeminiEngine(config, db)
