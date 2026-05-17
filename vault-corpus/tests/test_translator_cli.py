"""Sub-AC 4.4 smoke tests for the ``python -m vault_corpus.translator`` CLI.

The README's API walkthrough documents a copy-pasteable command for
inspecting a single translation in isolation. These tests execute that
exact command against a fixture chunk with a stubbed OpenAI client and
assert non-empty English output, plus a couple of structural guards on
the documented inspection flags.

Two execution paths are exercised:

* In-process via :func:`vault_corpus.translator._main` — fast, covers the
  argparse + stub-client wiring.
* Out-of-process via ``subprocess.run([sys.executable, "-m",
  "vault_corpus.translator", ...])`` — proves the documented copy-paste
  command actually works as printed, not just as a Python function.
"""

from __future__ import annotations

import subprocess
import sys
from io import StringIO

import pytest

from vault_corpus import translator as translator_mod


# ---------------------------------------------------------------------------
# In-process: _main(argv) returns 0 and prints non-empty English body.
# ---------------------------------------------------------------------------


def test_main_demo_default_prints_non_empty_english_body(capsys):
    rc = translator_mod._main(["--demo"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "=== translated body ===" in out
    # English body must be non-empty.
    body_marker = "=== translated body ===\n"
    body_idx = out.index(body_marker) + len(body_marker)
    tail = out[body_idx:]
    # First non-empty line after the marker is the translated body line.
    first_body_line = next(
        (ln for ln in tail.splitlines() if ln.strip() and not ln.startswith("===")),
        "",
    )
    assert first_body_line.strip(), "translator CLI emitted empty English body"
    # And it should look like English markdown (no Korean Hangul syllables).
    assert not any("가" <= ch <= "힯" for ch in first_body_line), (
        f"expected English output, found Korean: {first_body_line!r}"
    )


def test_main_demo_default_when_no_flags_given(capsys):
    # Sub-AC 4.4: documented command must work without --demo flag too, so
    # that a user pasting `python -m vault_corpus.translator` (no args) is
    # not surprised by a missing-key error.
    rc = translator_mod._main([])
    out = capsys.readouterr().out
    assert rc == 0
    assert "## Goal" in out


def test_main_show_payload_prints_request_payload(capsys):
    rc = translator_mod._main(["--demo", "--show-payload"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "=== request payload ===" in out
    # Required payload keys must appear in the printed JSON.
    for key in ('"model"', '"temperature"', '"seed"', '"messages"'):
        assert key in out, f"payload printout missing key: {key}"
    # Model default + temperature=0 + seed default present.
    assert translator_mod.DEFAULT_TRANSLATION_MODEL in out
    assert '"temperature": 0' in out


def test_main_show_prompt_prints_system_prompt_template(capsys):
    rc = translator_mod._main(["--demo", "--show-prompt"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "=== system prompt template ===" in out
    # A representative substring of the template (asserted exactly so a
    # silent template edit that breaks the README walkthrough is caught).
    assert "Korean-to-English translator" in out
    assert "code blocks verbatim" in out


def test_main_text_override_translates_arbitrary_input(capsys):
    rc = translator_mod._main(["--demo", "--text", "## 학습\n오늘"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "=== translated body ===" in out
    assert "stub translation" in out  # _DEMO stub branch for arbitrary text


def test_main_rejects_demo_and_live_together(capsys):
    rc = translator_mod._main(["--demo", "--live"])
    err = capsys.readouterr().err
    assert rc == 2
    assert "mutually exclusive" in err


def test_main_does_not_hit_network_in_demo_mode(monkeypatch):
    # Make `from openai import OpenAI` explode if the CLI accidentally
    # imports it during --demo. Sub-AC requires zero network calls.
    class _Boom:
        def __init__(self, *a, **kw):
            raise RuntimeError("OpenAI client must not be constructed in --demo mode")

    monkeypatch.setattr(translator_mod, "OpenAI", _Boom, raising=False)
    rc = translator_mod._main(["--demo"])
    assert rc == 0


# ---------------------------------------------------------------------------
# Out-of-process: the documented copy-paste command runs end-to-end.
# ---------------------------------------------------------------------------


def test_module_runs_via_python_dash_m_and_prints_english():
    """Sub-AC 4.4 gate: the documented command works as printed."""
    result = subprocess.run(
        [sys.executable, "-m", "vault_corpus.translator", "--demo"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, (
        f"CLI exited {result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "=== translated body ===" in result.stdout
    # Non-empty English output.
    assert "## Goal" in result.stdout
    assert "Write code daily." in result.stdout
    # Korean must not appear in the translated-body section (Hangul check).
    body_section = result.stdout.split("=== translated body ===", 1)[1]
    assert not any("가" <= ch <= "힯" for ch in body_section), (
        "translated body section contains untranslated Korean"
    )


def test_module_subprocess_show_payload_and_prompt():
    """Combined inspection flags work in the documented form."""
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "vault_corpus.translator",
            "--demo",
            "--show-payload",
            "--show-prompt",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0
    assert "=== system prompt template ===" in result.stdout
    assert "=== request payload ===" in result.stdout
    assert "=== translated body ===" in result.stdout


# ---------------------------------------------------------------------------
# README walkthrough integrity: the documented command must exist verbatim.
# ---------------------------------------------------------------------------


def test_readme_documents_the_inspection_command():
    from pathlib import Path

    readme = (Path(__file__).resolve().parent.parent / "README.md").read_text(
        encoding="utf-8"
    )
    # The exact copy-paste command in stub mode.
    assert "python -m vault_corpus.translator --demo" in readme
    # The payload-shape and prompt-template inspection flags must be
    # documented somewhere in the README.
    assert "--show-payload" in readme
    assert "--show-prompt" in readme
    # Payload shape (key list) must appear in the README so a reader can
    # see it without running anything.
    for key in ('"model"', '"temperature"', '"seed"', '"messages"'):
        assert key in readme, f"README missing payload key in walkthrough: {key}"


# ---------------------------------------------------------------------------
# Guard against accidental import-time side effects.
# ---------------------------------------------------------------------------


def test_importing_translator_does_not_invoke_cli():
    # Re-import in a fresh sub-interpreter-ish way: just call the module's
    # entry-point function manually and ensure it returns. Importing alone
    # must not write to stdout or touch the network.
    captured = StringIO()
    original_stdout = sys.stdout
    try:
        sys.stdout = captured
        import importlib

        importlib.reload(translator_mod)
    finally:
        sys.stdout = original_stdout
    assert captured.getvalue() == ""
