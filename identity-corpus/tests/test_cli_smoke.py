"""Smoke test for Sub-AC 2: CLI imports and --help exits 0."""

from __future__ import annotations

from typer.testing import CliRunner


def test_app_imports() -> None:
    from identity_corpus.cli import app

    assert app is not None


def test_help_exit_code_zero() -> None:
    from identity_corpus.cli import app

    runner = CliRunner()
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0


def test_subcommand_registered() -> None:
    from identity_corpus.cli import app

    runner = CliRunner()
    result = runner.invoke(app, ["version", "--help"])
    assert result.exit_code == 0


def test_new_subcommands_show_help() -> None:
    from identity_corpus.cli import app

    runner = CliRunner()
    commands = [
        ["build", "--help"],
        ["status", "--help"],
        ["tags", "list", "--help"],
        ["tags", "promote", "--help"],
        ["review", "export", "--help"],
        ["review", "import", "--help"],
        ["profile", "generate", "--help"],
        ["search", "--help"],
        ["validate", "--help"],
    ]
    for command in commands:
        result = runner.invoke(app, command)
        assert result.exit_code == 0, result.output
