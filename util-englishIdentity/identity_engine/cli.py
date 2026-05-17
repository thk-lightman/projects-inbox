"""CLI entry point — embedding-based pipeline with human review gate.

Stages (run separately):
    crawl     — tokenize sentences from user-specified paths into DB (lang-tagged)
    embed     — compute sentence-transformer vectors for sentences without one
    cluster   — incremental nearest-neighbor: attach to existing centroid or seed new
                (deterministic, cross-batch consistent, no LLM)
    label     — LLM names dirty clusters with canonical patterns + gloss
    review    — TSV export → human edits → import (status: pending → locked)
    translate — locked KR expressions → EN. KR instance sentences → EN.
                (no-op for lang=en: canonical is already EN)
    sync      — push translated expressions as Anki cards + Sheets rows

`run` chains crawl → embed → cluster → label (stops before review = human gate).
"""
import signal
import sys
from pathlib import Path
from typing import Optional

import click
from tqdm import tqdm

from .clusterer import Clusterer
from .config import load_config
from .database import (
    Database,
    LANG_EN,
    LANG_KR,
    STATUS_LOCKED,
    STATUS_TRANSLATED,
)
from .embedder import Embedder, to_blob
from .gemini_engine import make_engine
from .labeler import Labeler
from .review import export_for_review, import_reviewed
from .sync_manager import SyncManager
from .vault_crawler import crawl_vault

_shutdown_requested = False


def _handle_sigint(signum, frame):
    global _shutdown_requested
    click.echo("\n[!] Interrupt received — finishing current item and saving checkpoint…", err=True)
    _shutdown_requested = True


def _normalize_path_prefixes(vault_path: Path, paths: list[str]) -> list[str]:
    vault_resolved = vault_path.resolve()
    out: list[str] = []
    for raw in paths:
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = vault_path / raw
        resolved = candidate.resolve()
        try:
            rel = resolved.relative_to(vault_resolved)
        except ValueError as exc:
            raise click.ClickException(f"path outside vault: {raw}") from exc
        out.append(str(rel))
    return out


signal.signal(signal.SIGINT, _handle_sigint)
signal.signal(signal.SIGTERM, _handle_sigint)


@click.group()
@click.option("--env", default=".env", show_default=True, help="Path to .env file")
@click.pass_context
def cli(ctx: click.Context, env: str):
    """Identity-Engine v4.0 — embedding-based expression pipeline (KR + EN)."""
    ctx.ensure_object(dict)
    env_path = Path(env) if Path(env).exists() else None
    try:
        cfg = load_config(env_path)
    except KeyError as exc:
        raise click.ClickException(f"Missing required env var: {exc}") from exc
    ctx.obj["cfg"] = cfg
    ctx.obj["db"] = Database(cfg.db_path)


# ─────────────────────────────────────────────────────────────────────────────
# crawl
# ─────────────────────────────────────────────────────────────────────────────
@cli.command()
@click.option("--paths", "-p", multiple=True, required=True,
              help="Vault file or folder paths (repeatable). Required.")
@click.option("--mode", type=click.Choice([LANG_KR, LANG_EN]), default=LANG_KR,
              show_default=True)
@click.option("--force", is_flag=True)
@click.option("--resume/--no-resume", default=True, show_default=True)
@click.pass_context
def crawl(ctx: click.Context, paths: tuple, mode: str, force: bool, resume: bool):
    """Tokenize sentences from explicit paths into DB."""
    cfg = ctx.obj["cfg"]
    db: Database = ctx.obj["db"]
    path_list = list(paths)
    _normalize_path_prefixes(cfg.vault_path, path_list)

    with db:
        click.echo(f"▶  Crawling [mode={mode}] scope: {', '.join(path_list)}…")
        items = []
        try:
            for item in crawl_vault(cfg.vault_path, db, paths=path_list, lang=mode,
                                    force=force, resume=resume):
                items.append(item)
                if _shutdown_requested:
                    click.echo("   Checkpoint saved.")
                    sys.exit(0)
        except ValueError as exc:
            raise click.ClickException(str(exc)) from exc
        inserted = db.upsert_sentences_batch(items) if items else 0
        click.echo(f"   {len(items)} sentences found, {inserted} new.")
        db.clear_checkpoint()


# ─────────────────────────────────────────────────────────────────────────────
# embed
# ─────────────────────────────────────────────────────────────────────────────
@cli.command()
@click.option("--mode", type=click.Choice([LANG_KR, LANG_EN]), default=LANG_KR,
              show_default=True)
@click.option("--batch-size", default=None, type=int)
@click.pass_context
def embed(ctx: click.Context, mode: str, batch_size: Optional[int]):
    """Compute embeddings for sentences without one (deterministic, no LLM)."""
    cfg = ctx.obj["cfg"]
    db: Database = ctx.obj["db"]
    bs = batch_size or cfg.embed_batch_size

    with db:
        rows = db.get_sentences_without_embedding(lang=mode)
        if not rows:
            click.echo("   No sentences need embedding.")
            return
        click.echo(f"▶  Embedding {len(rows)} {mode} sentences (model={cfg.embedding_model})…")
        embedder = Embedder(cfg)
        bar = tqdm(total=len(rows), unit="sent", ncols=72)
        for i in range(0, len(rows), bs):
            chunk = rows[i : i + bs]
            texts = [r["kr_text"] for r in chunk]
            vecs = embedder.encode_batch(texts, batch_size=bs)
            db.set_sentence_embeddings_batch(
                [(r["id"], to_blob(vecs[idx])) for idx, r in enumerate(chunk)]
            )
            bar.update(len(chunk))
            if _shutdown_requested:
                bar.close(); click.echo("   Interrupted."); sys.exit(0)
        bar.close()
        click.echo(f"   Embedded {len(rows)} sentences.")


# ─────────────────────────────────────────────────────────────────────────────
# cluster
# ─────────────────────────────────────────────────────────────────────────────
@cli.command()
@click.option("--mode", type=click.Choice([LANG_KR, LANG_EN]), default=LANG_KR,
              show_default=True)
@click.option("--paths", "-p", multiple=True,
              help="Restrict to sentences whose source_file starts with these prefixes.")
@click.pass_context
def cluster(ctx: click.Context, mode: str, paths: tuple):
    """Incremental clustering: pending sentences → expressions (cross-batch aware)."""
    cfg = ctx.obj["cfg"]
    db: Database = ctx.obj["db"]
    rel_prefixes = _normalize_path_prefixes(cfg.vault_path, list(paths)) if paths else None

    with db:
        rows = db.get_sentences_with_embedding(lang=mode, statuses=["pending"])
        if rel_prefixes:
            rows = [r for r in rows if any(r["source_file"].startswith(p) for p in rel_prefixes)]
        if not rows:
            click.echo("   No pending+embedded sentences in scope.")
            return
        click.echo(
            f"▶  Clustering {len(rows)} {mode} sentences "
            f"(threshold={cfg.cluster_threshold})…"
        )
        clusterer = Clusterer(cfg, db, lang=mode)
        bar = tqdm(total=len(rows), unit="sent", ncols=72)
        result = clusterer.cluster_sentences(
            rows, on_progress=lambda *a, **kw: bar.update(1) if not kw.get("skipped") else None,
        )
        bar.close()
        click.echo(
            f"   Attached: {result['attached']}, created: {result['created']}, "
            f"skipped: {result['skipped']}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# label
# ─────────────────────────────────────────────────────────────────────────────
@cli.command()
@click.option("--mode", type=click.Choice([LANG_KR, LANG_EN]), default=LANG_KR,
              show_default=True)
@click.option("--min-members", default=1, show_default=True, type=int,
              help="Only label clusters with at least N members.")
@click.pass_context
def label(ctx: click.Context, mode: str, min_members: int):
    """LLM names dirty (new or changed) clusters with canonical patterns + gloss."""
    cfg = ctx.obj["cfg"]
    db: Database = ctx.obj["db"]
    with db:
        try:
            labeler = Labeler(cfg, db, lang=mode)
        except RuntimeError as exc:
            raise click.ClickException(str(exc)) from exc

        def on_progress(_id, error=None, info=None):
            if info: click.echo(f"   {info}")
            if error: click.echo(f"   [!] {error}")

        click.echo(f"▶  Labeling dirty {mode} clusters (min_members={min_members})…")
        ok, err = labeler.label_dirty(min_member_count=min_members, on_progress=on_progress)
        click.echo(f"   Labeled: {ok}, errors: {err}")


# ─────────────────────────────────────────────────────────────────────────────
# review
# ─────────────────────────────────────────────────────────────────────────────
@cli.group()
def review():
    """Human review gate: export TSV → edit → import."""


@review.command("export")
@click.option("--output", "-o", default="review.tsv", show_default=True)
@click.option("--mode", type=click.Choice([LANG_KR, LANG_EN]), default=None)
@click.option("--min-frequency", default=1, show_default=True, type=int)
@click.pass_context
def review_export(ctx: click.Context, output: str, mode: Optional[str], min_frequency: int):
    """Export pending expressions to TSV for editing."""
    db: Database = ctx.obj["db"]
    out_path = Path(output)
    with db:
        n = export_for_review(
            db, out_path, lang=mode, statuses=["pending"], min_freq=min_frequency,
        )
    click.echo(f"Exported {n} expressions → {out_path}")
    click.echo("Edit the TSV (keep=0 to drop, merge_into_id to merge, edit expr to rename),")
    click.echo(f"then run: python run.py review import {out_path}")


@review.command("import")
@click.argument("path", type=click.Path(exists=True, dir_okay=False))
@click.pass_context
def review_import(ctx: click.Context, path: str):
    """Apply edited TSV → kept rows become status=locked."""
    db: Database = ctx.obj["db"]
    with db:
        result = import_reviewed(db, Path(path))
    click.echo(
        f"Locked: {result['locked']}, deleted: {result['deleted']}, "
        f"merged: {result['merged']}, updated: {result['updated']}, "
        f"skipped: {result['skipped']}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# translate
# ─────────────────────────────────────────────────────────────────────────────
@cli.command()
@click.option("--mode", type=click.Choice([LANG_KR, LANG_EN]), default=LANG_KR,
              show_default=True)
@click.option("--min-frequency", default=1, show_default=True, type=int)
@click.option("--batch-size", default=None, type=int)
@click.pass_context
def translate(ctx: click.Context, mode: str, min_frequency: int, batch_size: Optional[int]):
    """Translate locked expressions + instance sentences (KR mode). No-op for EN."""
    cfg = ctx.obj["cfg"]
    db: Database = ctx.obj["db"]

    with db:
        locked = db.get_expressions_filtered(
            lang=mode, statuses=[STATUS_LOCKED], min_freq=min_frequency,
        )
        if not locked:
            click.echo("   No locked expressions to translate.")
            return

        if mode == LANG_EN:
            for e in locked:
                db.set_expression_translation(e["id"], e["en_expr"] or e["kr_expr"], e["gloss"])
            click.echo(f"   {len(locked)} EN expressions advanced to translated.")
            return

        try:
            engine = make_engine(cfg, db)
        except RuntimeError as exc:
            raise click.ClickException(str(exc)) from exc

        click.echo(f"▶  Translating {len(locked)} KR expressions…")
        bar = tqdm(total=len(locked), unit="expr", ncols=72)

        def on_expr(_id, error=None, info=None):
            if info: bar.write(f"   {info}"); return
            bar.update(1)
            if error: bar.write(f"   [!] {error}")

        ok, err = engine.translate_expressions(locked, on_progress=on_expr)
        bar.close()
        click.echo(f"   Expressions translated: {ok}, errors: {err}")

        translated = db.get_expressions_filtered(
            lang=mode, statuses=[STATUS_TRANSLATED], min_freq=min_frequency,
        )
        instances = db.get_pending_instance_sentences([e["id"] for e in translated])
        if instances:
            click.echo(f"▶  Translating {len(instances)} instance sentences…")
            bar = tqdm(total=len(instances), unit="sent", ncols=72)

            def on_inst(kr_hash, en_text=None, error=None, info=None):
                if info: bar.write(f"   {info}"); return
                bar.update(1)
                if error: bar.write(f"   [!] {(kr_hash or '')[:8]}… {error}")
                if _shutdown_requested:
                    bar.close(); sys.exit(0)

            ok2, err2 = engine.translate_pending(
                batch_size=batch_size, on_progress=on_inst, rows=instances,
            )
            bar.close()
            click.echo(f"   Instances translated: {ok2}, errors: {err2}")


# ─────────────────────────────────────────────────────────────────────────────
# sync
# ─────────────────────────────────────────────────────────────────────────────
@cli.command()
@click.option("--mode", type=click.Choice([LANG_KR, LANG_EN]), default=None)
@click.option("--min-frequency", default=1, show_default=True, type=int)
@click.pass_context
def sync(ctx: click.Context, mode: Optional[str], min_frequency: int):
    """Push translated expressions to Anki + Sheets."""
    cfg = ctx.obj["cfg"]
    db: Database = ctx.obj["db"]
    with db:
        rows = db.get_expressions_filtered(
            lang=mode, statuses=[STATUS_TRANSLATED], min_freq=min_frequency,
        )
        if not rows:
            click.echo("   Nothing to sync.")
            return
        click.echo(f"▶  Syncing {len(rows)} expressions to Anki/Sheets…")
        bar = tqdm(total=len(rows), unit="card", ncols=72)
        syncer = SyncManager(cfg, db)
        anki_ok, anki_err = syncer.push_expressions_to_anki(
            rows, on_progress=lambda *_: bar.update(1),
        )
        sheets_n = syncer.push_expressions_to_sheets(rows)
        bar.close()
        click.echo(f"   Anki added: {anki_ok}, errors: {anki_err}, sheets rows: {sheets_n}")


# ─────────────────────────────────────────────────────────────────────────────
# run (chained)
# ─────────────────────────────────────────────────────────────────────────────
@cli.command()
@click.option("--paths", "-p", multiple=True, required=True)
@click.option("--mode", type=click.Choice([LANG_KR, LANG_EN]), default=LANG_KR,
              show_default=True)
@click.option("--stop-after", type=click.Choice(["crawl", "embed", "cluster", "label"]),
              default="label", show_default=True)
@click.option("--force", is_flag=True)
@click.option("--resume/--no-resume", default=True, show_default=True)
@click.pass_context
def run(ctx: click.Context, paths: tuple, mode: str, stop_after: str,
        force: bool, resume: bool):
    """Convenience: crawl → embed → cluster → label. Stops before review."""
    ctx.invoke(crawl, paths=paths, mode=mode, force=force, resume=resume)
    if stop_after == "crawl": return
    ctx.invoke(embed, mode=mode, batch_size=None)
    if stop_after == "embed": return
    ctx.invoke(cluster, mode=mode, paths=paths)
    if stop_after == "cluster": return
    ctx.invoke(label, mode=mode, min_members=1)
    click.echo("")
    click.echo("Next (manual):")
    click.echo(f"  python run.py review export -o review-{mode}.tsv --mode {mode}")
    click.echo("  # edit the TSV")
    click.echo(f"  python run.py review import review-{mode}.tsv")
    click.echo(f"  python run.py translate --mode {mode}")
    click.echo(f"  python run.py sync --mode {mode}")


# ─────────────────────────────────────────────────────────────────────────────
# status & export
# ─────────────────────────────────────────────────────────────────────────────
@cli.command()
@click.pass_context
def status(ctx: click.Context):
    """Show pipeline status."""
    db: Database = ctx.obj["db"]
    with db:
        sent_stats = db.stats()
        expr_stats = db.expression_stats()
        embedded = db._conn.execute(
            "SELECT COUNT(*) AS c FROM sentences WHERE embedding IS NOT NULL"
        ).fetchone()["c"]
        dirty = db._conn.execute(
            "SELECT COUNT(*) AS c FROM expressions WHERE label_dirty=1"
        ).fetchone()["c"]

    click.echo("── Sentences ──")
    total_s = sum(sent_stats.values())
    for s in ("pending", "curated", "translated", "synced", "error"):
        cnt = sent_stats.get(s, 0)
        pct = cnt / total_s * 100 if total_s else 0
        click.echo(f"  {s:<14} {cnt:>6}  {pct:>4.1f}%")
    click.echo(f"  {'TOTAL':<14} {total_s:>6}")
    click.echo(f"  embedded:     {embedded:>6}")

    click.echo("── Expressions ──")
    total_e = sum(expr_stats.values())
    for s in ("pending", "locked", "translated", "synced", "error"):
        cnt = expr_stats.get(s, 0)
        pct = cnt / total_e * 100 if total_e else 0
        click.echo(f"  {s:<14} {cnt:>6}  {pct:>4.1f}%")
    click.echo(f"  {'TOTAL':<14} {total_e:>6}")
    click.echo(f"  label_dirty:  {dirty:>6}")


@cli.command()
@click.option("--output", "-o", default="export.tsv", show_default=True)
@click.option("--kind", default="expressions",
              type=click.Choice(["expressions", "sentences"]))
@click.option("--mode", type=click.Choice([LANG_KR, LANG_EN]), default=None)
@click.option("--stage", default="all",
              type=click.Choice(["pending", "curated", "locked",
                                 "translated", "synced", "error", "all"]))
@click.pass_context
def export(ctx: click.Context, output: str, kind: str, mode: Optional[str], stage: str):
    """Export expressions or sentences to TSV."""
    db: Database = ctx.obj["db"]
    out_path = Path(output)
    with db:
        if kind == "expressions":
            statuses = None if stage == "all" else [stage]
            rows = db.get_expressions_filtered(lang=mode, statuses=statuses)
            with out_path.open("w", encoding="utf-8") as f:
                f.write("id\tlang\tfreq\tmember_count\tstatus\tkr_expr\ten_expr\tgloss\n")
                for r in rows:
                    f.write(
                        f"{r['id']}\t{r['lang']}\t{r['freq']}\t{r['member_count']}\t"
                        f"{r['status']}\t{r['kr_expr']}\t{r['en_expr'] or ''}\t{r['gloss'] or ''}\n"
                    )
        else:
            statuses = (
                ["pending", "curated", "translated", "synced", "error"]
                if stage == "all" else [stage]
            )
            rows = db.get_sentences_by_status(*statuses)
            if mode:
                rows = [r for r in rows if r["lang"] == mode]
            with out_path.open("w", encoding="utf-8") as f:
                f.write("id\tlang\tstatus\tkr_text\ten_text\tsource_file\n")
                for r in rows:
                    f.write(
                        f"{r['id']}\t{r['lang']}\t{r['status']}\t"
                        f"{r['kr_text']}\t{r['en_text'] or ''}\t{r['source_file']}\n"
                    )
    click.echo(f"Exported {len(rows)} rows → {out_path}")
