"""Typer-based CLI entrypoint.

Subcommand groups:

* ``version`` — print installed package version.
* ``status``  — read-only inspection of the chunk DB.
* ``build``   — full scan → chunk → translate → embed → upsert pipeline.
* ``moc generate`` — cluster English chunks and write top-``n`` MOC samples.

Smoke-test subcommand is wired by sibling AC8.

Module-level symbols ``OpenAI``, ``build_pipeline``, and ``init_db`` are
imported here (rather than inside the command body) so unit tests can
monkeypatch them via ``vault_corpus.cli.<name>`` without touching the
upstream packages. This is the seam the AC6.2 tests use to assert the
``build`` command sequences the pipeline correctly without burning OpenAI
API calls or hitting the real vault.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import typer

from vault_corpus.pipeline import (
    build_pipeline,
    git_diff_changed_files,
    process_delta,
)
from vault_corpus.smoke import (
    DEFAULT_MIN_RESULTS,
    DEFAULT_SIMILARITY_FLOOR,
    evaluate_results,
    format_results,
    load_smoke_queries,
    run_query,
)
from vault_corpus.store import init_db

try:
    from openai import OpenAI  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover — surfaced at runtime, not import time
    OpenAI = None  # type: ignore[assignment]

app = typer.Typer(
    name="vault-corpus",
    help="Mirror Korean Obsidian vault into English chunked corpus + vector index.",
    no_args_is_help=True,
)


moc_app = typer.Typer(
    name="moc",
    help="Topic clustering + Map-of-Content sample generation.",
    no_args_is_help=True,
)
app.add_typer(moc_app, name="moc")


@app.callback()
def _main() -> None:
    """vault-corpus: Korean Obsidian vault → English chunked corpus + vector index."""


@app.command()
def version() -> None:
    """Print installed package version."""
    from vault_corpus import __version__

    typer.echo(__version__)


# Default DB path mirrors the build pipeline target (project-relative ``data/``).
# Status is read-only — the path is opened (and the empty schema created on
# first run by :func:`init_db`) but no chunks are written by this command.
_DEFAULT_DB_PATH = Path("data/corpus.db")

# Default vault root — the user's Korean Obsidian vault. Scanner restricts
# enumeration to the 5 in-scope top-level directories; the path is read-only
# in every code path (see seed contract: vault is immutable source-of-truth).
_DEFAULT_VAULT_PATH = Path("/Users/mori/Obsidian/Obsidian_Master_v2")


@app.command()
def status(
    db: Path = typer.Option(
        _DEFAULT_DB_PATH,
        "--db",
        help="Path to the SQLite chunk store.",
        show_default=True,
    ),
) -> None:
    """Print chunk counts grouped by lang, distinct source file count, and last build timestamp.

    Read-only inspection of the corpus DB. Counts are computed with three small
    SQL queries against the ``chunks`` table written by the build pipeline:

    * ``SELECT lang, COUNT(*) FROM chunks GROUP BY lang`` — per-language totals
      (``ko`` for source chunks, ``en`` for translations).
    * ``SELECT COUNT(DISTINCT source_path) FROM chunks`` — how many source
      notes are represented in the index, irrespective of how many chunks each
      contributed.
    * ``SELECT MAX(build_ts) FROM chunks`` — ISO-8601 timestamp of the most
      recent build pass to touch any row.

    No write occurs. :func:`init_db` is invoked so that pointing ``--db`` at a
    not-yet-built location yields a usable empty-DB summary instead of an
    error — useful as a sanity check before the first ``build`` run.
    """
    from vault_corpus.store import init_db

    conn = init_db(db)
    try:
        lang_rows = conn.execute(
            "SELECT lang, COUNT(*) FROM chunks GROUP BY lang ORDER BY lang"
        ).fetchall()
        last_build_ts = conn.execute(
            "SELECT MAX(build_ts) FROM chunks"
        ).fetchone()[0]
        file_count = conn.execute(
            "SELECT COUNT(DISTINCT source_path) FROM chunks"
        ).fetchone()[0]
    finally:
        conn.close()

    total_chunks = sum(count for _lang, count in lang_rows)

    typer.echo(f"db: {db}")
    typer.echo(f"total chunks: {total_chunks}")
    typer.echo("chunks by lang:")
    if lang_rows:
        for lang, count in lang_rows:
            typer.echo(f"  {lang}: {count}")
    else:
        typer.echo("  (none)")
    typer.echo(f"distinct source files: {file_count}")
    typer.echo(f"last build_ts: {last_build_ts if last_build_ts else '(never)'}")


# ---------------------------------------------------------------------------
# build
# ---------------------------------------------------------------------------


def _make_openai_client() -> Any:
    """Construct a real :class:`openai.OpenAI` client.

    Isolated behind a function so the ``build`` command stays declarative
    and so tests can monkeypatch ``vault_corpus.cli._make_openai_client``
    (or just ``vault_corpus.cli.OpenAI``) without instantiating the real
    SDK class, which would otherwise demand a valid ``OPENAI_API_KEY``
    in the environment at import or call time.
    """
    if OpenAI is None:
        raise RuntimeError(
            "openai package not installed; run `pip install openai` first."
        )
    return OpenAI()


@app.command()
def build(
    vault_path: Path = typer.Option(
        _DEFAULT_VAULT_PATH,
        "--vault-path",
        help="Root of the Korean Obsidian vault (read-only).",
    ),
    db: Path = typer.Option(
        _DEFAULT_DB_PATH,
        "--db",
        help="Path to the SQLite chunk store (created on first run).",
    ),
    delta: bool = typer.Option(
        False,
        "--delta",
        help=(
            "Incremental rebuild driven by `git diff --name-only HEAD~1` "
            "inside the vault repo. Only re-processes files surfaced by "
            "git diff that fall within the 5 scoped vault directories."
        ),
    ),
    ref: str = typer.Option(
        "HEAD~1",
        "--ref",
        help="Git ref to diff against in --delta mode (default HEAD~1).",
    ),
) -> None:
    """Run a full or delta scan → chunk → translate → embed → upsert build.

    Without ``--delta``: scans the full vault, chunks every in-scope note,
    and translates+embeds every chunk whose content-derived ``chunk_id``
    does not already have an English mirror with an embedding.

    With ``--delta``: enumerates files via ``git diff --name-only
    <ref>`` inside ``vault_path``, filters to in-scope markdown files,
    deletes obsolete chunks (heading was renamed / merged / removed) per
    file, then translates+embeds only the new chunks. Zero API calls
    when no in-scope file changed.

    The vault is read-only in every code path. Cost-tracker output prints
    total OpenAI call counts and an estimated USD figure at end of run.
    """
    translate_client = _make_openai_client()
    embed_client = translate_client

    conn = init_db(db)
    try:
        if delta:
            changed = git_diff_changed_files(vault_path, ref=ref)
            typer.echo(
                f"delta: {len(changed)} in-scope file(s) changed since {ref}"
            )
            report = process_delta(
                conn,
                changed,
                translate_client,
                embed_client,
            )
        else:
            report = build_pipeline(
                vault_path,
                conn,
                translate_client,
                embed_client,
            )
    finally:
        conn.close()

    typer.echo(f"files scanned: {report.files_scanned}")
    typer.echo(f"chunks seen: {report.chunks_seen}")
    typer.echo(f"chunks translated: {report.chunks_translated}")
    typer.echo(f"chunks embedded: {report.chunks_embedded}")
    typer.echo(f"chunks upserted: {report.chunks_upserted}")
    typer.echo(f"skipped existing: {report.skipped_existing}")
    typer.echo(f"chunks deleted: {report.chunks_deleted}")
    typer.echo(f"failed files: {len(report.failed_files)}")
    for line in report.cost.summary_lines():
        typer.echo(line)


# ---------------------------------------------------------------------------
# moc generate
# ---------------------------------------------------------------------------


# Default MOC output dir lives under the project repo's ``data/`` tree, never
# inside the Obsidian vault. The seed contract requires all generated MOC
# markdown to live outside the vault.
_DEFAULT_MOC_OUT = Path("data/moc_samples")


@moc_app.command("generate")
def moc_generate(
    n: int = typer.Option(10, "--n", help="Number of cluster MOCs to generate."),
    db: Path = typer.Option(
        _DEFAULT_DB_PATH,
        "--db",
        help="Path to the vault-corpus SQLite database.",
    ),
    out_dir: Path = typer.Option(
        _DEFAULT_MOC_OUT,
        "--out-dir",
        help="Directory to write MOC-*.md files into (must live OUTSIDE the vault).",
    ),
    algo: str = typer.Option(
        "hdbscan",
        "--algo",
        help="Clustering algorithm: 'hdbscan' (default) or 'kmeans'.",
    ),
    min_cluster_size: int = typer.Option(
        10,
        "--min-cluster-size",
        help="HDBSCAN min_cluster_size (ignored for k-means).",
    ),
    min_samples: int = typer.Option(
        5,
        "--min-samples",
        help="HDBSCAN min_samples (ignored for k-means).",
    ),
    top_k: int = typer.Option(
        5,
        "--top-k",
        help="Number of central chunks listed per MOC.",
    ),
    vault_root: Optional[Path] = typer.Option(
        None,
        "--vault-root",
        help=(
            "Obsidian vault root. When set, the command refuses to write "
            "anywhere inside it — guarantees vault immutability."
        ),
    ),
    no_llm: bool = typer.Option(
        False,
        "--no-llm",
        help=(
            "Skip OpenAI calls for topic title / summary; use deterministic "
            "heading-derived fallbacks instead. Useful for offline runs and "
            "for inspecting the clustering output without burning tokens."
        ),
    ),
    model: Optional[str] = typer.Option(
        None,
        "--model",
        help="OpenAI chat model id for MOC title + summary (default: gpt-4o-mini).",
    ),
) -> None:
    """Cluster English chunks and write top-``n`` MOC samples.

    Loads every English chunk + embedding from ``--db``, runs HDBSCAN (or
    k-means via ``--algo kmeans``), picks the ``--n`` largest non-noise
    clusters, and writes one ``MOC-<slug>.md`` per cluster into ``--out-dir``.

    Every MOC file lives under ``--out-dir`` only. Nothing is ever written
    into the Obsidian vault — when ``--vault-root`` is supplied the command
    explicitly refuses to write inside it.
    """
    import sqlite3

    from vault_corpus import cluster as cluster_mod

    if not db.exists():
        typer.echo(f"error: database not found at {db}", err=True)
        raise typer.Exit(code=2)

    client = None
    effective_model = model or cluster_mod.DEFAULT_MOC_MODEL
    if not no_llm:
        try:
            from openai import OpenAI

            client = OpenAI()
        except Exception as exc:  # noqa: BLE001 — surface as CLI warning, fall back
            typer.echo(
                f"warning: OpenAI client unavailable ({exc}); falling back to --no-llm",
                err=True,
            )
            client = None

    conn = sqlite3.connect(str(db))
    try:
        results = cluster_mod.generate_mocs(
            conn,
            out_dir=out_dir,
            n=n,
            algo=algo,
            min_cluster_size=min_cluster_size,
            min_samples=min_samples,
            top_k=top_k,
            client=client,
            model=effective_model,
            vault_root=vault_root,
        )
    finally:
        conn.close()

    if not results:
        typer.echo("no MOCs generated (no English chunks with embeddings found)")
        raise typer.Exit(code=1)

    typer.echo(f"wrote {len(results)} MOC files to {out_dir}:")
    for r in results:
        typer.echo(f"  - {r.path} (cluster_id={r.cluster_id}, size={r.cluster_size})")


# ---------------------------------------------------------------------------
# smoke-test
# ---------------------------------------------------------------------------


@app.command("smoke-test")
def smoke_test(
    db: Path = typer.Option(
        _DEFAULT_DB_PATH,
        "--db",
        help="Path to the vault-corpus SQLite database.",
    ),
    queries_path: Optional[Path] = typer.Option(
        None,
        "--queries",
        help=(
            "Path to a smoke-queries YAML file. When omitted, the packaged "
            "default (smoke_queries.yaml) ships with 5 queries covering the "
            "major topical lobes of the vault."
        ),
    ),
    floor: float = typer.Option(
        DEFAULT_SIMILARITY_FLOOR,
        "--floor",
        help=(
            "Similarity floor for the gate. A query passes when at least "
            "--min-count results have cosine similarity >= floor. Inclusive."
        ),
    ),
    min_count: int = typer.Option(
        DEFAULT_MIN_RESULTS,
        "--min-count",
        help="Minimum above-floor results required per query to pass the gate.",
    ),
    top_k: int = typer.Option(
        5,
        "--top-k",
        help="Maximum results to retrieve per query.",
    ),
) -> None:
    """Run the smoke-test gate against the English chunk index.

    Loads queries (from ``--queries`` or the packaged default), runs each
    query against the vector index via :func:`vault_corpus.smoke.run_query`,
    evaluates each result set against the ``(floor, min_count)`` gate, and
    prints per-query :func:`format_results` blocks followed by an aggregated
    pass/fail summary line.

    Exits ``0`` when every query passes the gate; exits ``1`` when any one
    query fails. Designed to gate CI / "is the corpus usable" smoke runs —
    failure of a single query is sufficient to fail the build.
    """
    if not db.exists():
        typer.echo(f"error: database not found at {db}", err=True)
        raise typer.Exit(code=2)

    try:
        queries = load_smoke_queries(queries_path)
    except (FileNotFoundError, Exception) as exc:  # noqa: BLE001
        typer.echo(f"error: failed to load smoke queries: {exc}", err=True)
        raise typer.Exit(code=2)

    client = _make_openai_client()
    conn = init_db(db)

    pass_count = 0
    fail_count = 0
    try:
        for q in queries:
            query_text = q["query"]
            results = run_query(conn, query_text, top_k=top_k, client=client)
            passed, count_above = evaluate_results(
                results, floor=floor, min_count=min_count
            )
            typer.echo(format_results(query_text, results, top_k=top_k))
            verdict = "PASS" if passed else "FAIL"
            typer.echo(
                f"  → {verdict}: {count_above}/{min_count} results "
                f"above floor {floor:.4f}"
            )
            typer.echo("")
            if passed:
                pass_count += 1
            else:
                fail_count += 1
    finally:
        conn.close()

    total = pass_count + fail_count
    typer.echo(
        f"smoke-test: {pass_count}/{total} queries passed "
        f"(floor={floor:.4f}, min_count={min_count}, top_k={top_k})"
    )
    if fail_count > 0:
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
