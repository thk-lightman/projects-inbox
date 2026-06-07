"""identity-corpus CLI entry point."""

from __future__ import annotations

import json
import os
from pathlib import Path

import typer

from identity_corpus.corpus import read_corpus_json, write_corpus_json, write_corpus_yaml
from identity_corpus.examples import generate_examples
from identity_corpus.profile import export_review_tsv, generate_profile, import_review_tsv
from identity_corpus.sheets import push_to_sheet, write_staging_csv
from identity_corpus.scanner import scan_notes
from identity_corpus.store import connect, init_db, upsert_sentence
from identity_corpus.tagger import (
    apply_tags,
    load_taxonomy,
    promote_suggested_tag,
    reject_suggested_tag,
    tag_sentence,
    taxonomy_leaf_map,
)
from identity_corpus.tokenizer import sentence_id, tokenize_sentences
from identity_corpus.translator import translate_sentence
from identity_corpus.validation import validate_corpus_json

try:  # .env wiring for sheets push (sheet name, SA path); optional in dry-run.
    from dotenv import load_dotenv

    load_dotenv()
except Exception:
    pass

app = typer.Typer(
    name="identity-corpus",
    help=(
        "Turn curated IDENTITY/kr-self + IDENTITY/en-ref notes into a "
        "deduplicated sentence bank and a regenerable voice_profile.md."
    ),
    no_args_is_help=True,
    add_completion=False,
)
tags_app = typer.Typer(help="List, promote, or reject taxonomy tag suggestions.")
review_app = typer.Typer(help="Export or import operator review TSV files.")
profile_app = typer.Typer(help="Generate voice_profile.md.")
examples_app = typer.Typer(help="Generate English example sentences for tagged sentences.")
sheets_app = typer.Typer(help="Export example rows to a Google Sheets staging tab.")
app.add_typer(tags_app, name="tags")
app.add_typer(review_app, name="review")
app.add_typer(profile_app, name="profile")
app.add_typer(examples_app, name="examples")
app.add_typer(sheets_app, name="sheets")

DEFAULT_VAULT_ROOT = Path("~/Obsidian/Obsidian_Master_v2").expanduser()
DEFAULT_DB = Path("data/sentence_bank.db")
DEFAULT_TAXONOMY = Path("taxonomy.yaml")


@app.callback()
def _root() -> None:
    """Root callback so `identity-corpus --help` exits 0."""


@app.command("version")
def version() -> None:
    """No-op subcommand: print the package version."""
    from identity_corpus import __version__

    typer.echo(__version__)


def _openai_client():
    """Create an OpenAI client only when an API key is configured."""

    if not os.environ.get("OPENAI_API_KEY"):
        return None
    try:
        from openai import OpenAI

        return OpenAI()
    except Exception:
        return None


@app.command("build")
def build(
    vault_root: Path = typer.Option(DEFAULT_VAULT_ROOT, "--vault-root", help="Obsidian vault root."),
    db_path: Path = typer.Option(DEFAULT_DB, "--db", help="Sentence bank SQLite path."),
    taxonomy_path: Path = typer.Option(DEFAULT_TAXONOMY, "--taxonomy", help="taxonomy.yaml path."),
    delta: bool = typer.Option(False, "--delta", help="Reserved for git-diff incremental builds."),
    ref: str = typer.Option("HEAD~1", "--ref", help="Git ref for --delta."),
) -> None:
    """Run scan, tokenize, tag, and translate steps (per sentence)."""

    del ref  # The scope filter is implemented; git-diff plumbing is a v1 skeleton.
    db = init_db(db_path)
    client = _openai_client()
    taxonomy = load_taxonomy(taxonomy_path)
    processed = 0
    for note in scan_notes(vault_root):
        for sentence in tokenize_sentences(note.text, note.lang):
            sid = sentence_id(sentence, note.path)
            existing = db.execute(
                "SELECT file_fingerprint FROM sentences WHERE sentence_id=?", (sid,)
            ).fetchone()
            if existing and existing["file_fingerprint"] == note.fingerprint:
                continue
            upsert_sentence(
                db,
                sentence_id=sid,
                source_path=str(note.path),
                origin_lang=note.lang,
                text=sentence,
                file_fingerprint=note.fingerprint,
            )
            processed += 1

    if client:
        for row in db.execute(
            "SELECT sentence_id, tags_json FROM sentences ORDER BY sentence_id"
        ).fetchall():
            if not json.loads(row["tags_json"] or "{}"):
                apply_tags(db, row["sentence_id"], tag_sentence(db, row["sentence_id"], client, taxonomy))
            translate_sentence(db, row["sentence_id"], client)
    typer.echo(f"processed_sentences={processed} delta={delta}")


@app.command("status")
def status(db_path: Path = typer.Option(DEFAULT_DB, "--db")) -> None:
    """Print sentence bank statistics."""

    db = connect(db_path)
    counts = {
        row["status"]: row["count"]
        for row in db.execute("SELECT status, COUNT(*) AS count FROM sentences GROUP BY status")
    }
    tagged = db.execute(
        "SELECT COUNT(*) AS count FROM sentences WHERE tags_json NOT IN ('{}', '')"
    ).fetchone()["count"]
    suggested = db.execute("SELECT COUNT(*) AS count FROM suggested_tags").fetchone()["count"]
    last_build = db.execute("SELECT MAX(build_ts) AS ts FROM sentences").fetchone()["ts"]
    taxonomy_size = sum(len(leaves) for leaves in taxonomy_leaf_map(load_taxonomy(DEFAULT_TAXONOMY)).values())
    typer.echo(
        json.dumps(
            {
                "sentences": counts,
                "tagged": tagged,
                "last_build_ts": last_build,
                "taxonomy_leaves": taxonomy_size,
                "suggested_tags": suggested,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )


@tags_app.command("list")
def tags_list(
    db_path: Path = typer.Option(DEFAULT_DB, "--db"),
    taxonomy_path: Path = typer.Option(DEFAULT_TAXONOMY, "--taxonomy"),
) -> None:
    """Print current taxonomy leaves and pending suggestions."""

    taxonomy = taxonomy_leaf_map(load_taxonomy(taxonomy_path))
    db = init_db(db_path)
    suggestions = [dict(row) for row in db.execute("SELECT * FROM suggested_tags").fetchall()]
    typer.echo(json.dumps({"taxonomy": {k: sorted(v) for k, v in taxonomy.items()}, "suggestions": suggestions}, ensure_ascii=False, indent=2))


@tags_app.command("promote")
def tags_promote(
    dimension: str,
    tag: str,
    db_path: Path = typer.Option(DEFAULT_DB, "--db"),
    taxonomy_path: Path = typer.Option(DEFAULT_TAXONOMY, "--taxonomy"),
) -> None:
    """Promote a suggested tag into taxonomy.yaml."""

    promote_suggested_tag(init_db(db_path), dimension, tag, taxonomy_path=taxonomy_path)
    typer.echo("promoted")


@tags_app.command("reject")
def tags_reject(
    dimension: str,
    tag: str,
    db_path: Path = typer.Option(DEFAULT_DB, "--db"),
) -> None:
    """Reject a suggested tag."""

    reject_suggested_tag(init_db(db_path), dimension, tag)
    typer.echo("rejected")


@review_app.command("export")
def review_export(
    out: Path = typer.Option(Path("data/review.tsv"), "--out"),
    db_path: Path = typer.Option(DEFAULT_DB, "--db"),
) -> None:
    """Export review TSV."""

    export_review_tsv(connect(db_path), out)
    typer.echo(str(out))


@review_app.command("import")
def review_import(path: Path, db_path: Path = typer.Option(DEFAULT_DB, "--db")) -> None:
    """Import edited review TSV and apply operator transitions."""

    import_review_tsv(connect(db_path), path)
    typer.echo("imported")


@profile_app.command("generate")
def profile_generate(
    out: Path = typer.Option(Path("data/voice_profile.md"), "--out"),
    db_path: Path = typer.Option(DEFAULT_DB, "--db"),
) -> None:
    """Regenerate voice_profile.md."""

    generate_profile(connect(db_path), out)
    typer.echo(str(out))


@examples_app.command("generate")
def examples_generate(
    k: int = typer.Option(3, "--k", help="Examples per tagged cluster."),
    db_path: Path = typer.Option(DEFAULT_DB, "--db"),
) -> None:
    """Generate English example sentences for every tagged cluster (idempotent)."""

    client = _openai_client()
    if client is None:
        typer.echo("OPENAI_API_KEY not set; cannot generate examples.", err=True)
        raise typer.Exit(1)
    db = connect(db_path)
    total = 0
    for row in db.execute(
        "SELECT sentence_id FROM sentences WHERE tags_json NOT IN ('{}', '') ORDER BY sentence_id"
    ).fetchall():
        total += len(generate_examples(db, row["sentence_id"], client, k=k))
    typer.echo(f"examples={total}")


@sheets_app.command("csv")
def sheets_csv(
    out: Path = typer.Option(Path("data/staging.csv"), "--out"),
    db_path: Path = typer.Option(DEFAULT_DB, "--db"),
    all_rows: bool = typer.Option(False, "--all", help="Include already-staged sentences."),
) -> None:
    """Write the staging CSV artifact (does not mark sentences staged)."""

    count = write_staging_csv(connect(db_path), out, only_unstaged=not all_rows)
    typer.echo(f"{out} rows={count}")


@sheets_app.command("push")
def sheets_push(
    sheet: str = typer.Option(lambda: os.environ.get("ENGLISH_SHEET_NAME", ""), "--sheet", help="GS document name."),
    worksheet: str = typer.Option(lambda: os.environ.get("STAGING_WORKSHEET", "staging"), "--worksheet"),
    sa_file: str = typer.Option(lambda: os.environ.get("GCP_SERVICE_ACCOUNT_FILE", ""), "--sa-file", help="Service account JSON path."),
    db_path: Path = typer.Option(DEFAULT_DB, "--db"),
    all_rows: bool = typer.Option(False, "--all", help="Re-push already-staged sentences."),
) -> None:
    """Append unstaged example rows to the GS staging tab (marks them staged)."""

    if not sheet or not sa_file:
        typer.echo("Set --sheet/ENGLISH_SHEET_NAME and --sa-file/GCP_SERVICE_ACCOUNT_FILE.", err=True)
        raise typer.Exit(1)
    count = push_to_sheet(
        connect(db_path),
        sheet_name=sheet,
        service_account_file=sa_file,
        worksheet=worksheet,
        only_unstaged=not all_rows,
    )
    typer.echo(f"appended={count}")


@app.command("search")
def search(
    query: str,
    tag: str | None = typer.Option(None, "--tag", help="Filter as axis:leaf."),
    status_filter: str | None = typer.Option(None, "--status", help="Filter review status."),
    db_path: Path = typer.Option(DEFAULT_DB, "--db"),
) -> None:
    """Search sentence text with optional tag and status filters."""

    db = connect(db_path)
    params: list[str] = [f"%{query}%"]
    sql = """
        SELECT sentence_id, status, COALESCE(en_text, kr_text, en_translation) AS text,
               tags_json
        FROM sentences
        WHERE COALESCE(en_text, kr_text, en_translation) LIKE ?
    """
    if status_filter:
        sql += " AND status=?"
        params.append(status_filter)
    rows = db.execute(sql, params).fetchall()
    for row in rows:
        tags = json.loads(row["tags_json"] or "{}")
        if tag:
            axis, _, leaf = tag.partition(":")
            if tags.get(axis) != leaf:
                continue
        typer.echo(f"{row['sentence_id']}\t{row['status']}\t{row['text']}")


@app.command("validate")
def validate(
    path: Path,
    taxonomy_path: Path = typer.Option(DEFAULT_TAXONOMY, "--taxonomy"),
) -> None:
    """Validate a Corpus JSON file against taxonomy.yaml."""

    errors = validate_corpus_json(path, taxonomy_path)
    if errors:
        for error in errors:
            typer.echo(error, err=True)
        raise typer.Exit(1)
    typer.echo("valid")


@app.command("serialize")
def serialize(
    source_json: Path,
    out_yaml: Path | None = typer.Option(None, "--yaml-out"),
) -> None:
    """Round-trip canonical Corpus JSON and optionally emit YAML."""

    corpus = read_corpus_json(source_json)
    write_corpus_json(corpus, source_json)
    if out_yaml:
        write_corpus_yaml(corpus, out_yaml)
    typer.echo("serialized")


if __name__ == "__main__":
    app()
