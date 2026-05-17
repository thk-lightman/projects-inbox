"""Sub-AC 1.3: verify typer CLI entrypoint responds to --help."""

from typer.testing import CliRunner

from vault_corpus.cli import app

runner = CliRunner()


def test_help_exit_code_zero() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0


def test_help_mentions_command_name() -> None:
    result = runner.invoke(app, ["--help"], prog_name="vault-corpus")
    assert result.exit_code == 0
    assert "vault-corpus" in result.output


def test_help_lists_version_subcommand() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "version" in result.output


def test_version_subcommand_runs() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert result.output.strip() != ""
