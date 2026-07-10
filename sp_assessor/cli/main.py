"""sp-assessor CLI entry point."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from sp_assessor.core.config import Config, default_config, load_config
from sp_assessor.core.logging import setup_stage_logger
from sp_assessor.core.paths import ProjectPaths
from sp_assessor.stages import (
    diff as diff_stage,
    override_lint as override_lint_stage,
    s1_inventory,
    s2_metrics,
    s3_graph,
    s4_scoring,
    s5_report,
    spike as spike_stage,
    validate as validate_stage,
)


app = typer.Typer(add_completion=False, help="Oracle SP → Backend 전환평가 도구")
console = Console()


def _load(root: Path, config_path: Optional[Path]) -> tuple[ProjectPaths, Config]:
    paths = ProjectPaths.from_root(root)
    paths.ensure()
    cfg_path = config_path or paths.config
    if cfg_path.exists():
        cfg = load_config(cfg_path)
    else:
        console.print(f"[yellow]config not found ({cfg_path}); using defaults[/yellow]")
        cfg = default_config()
    return paths, cfg


@app.command()
def validate(
    root: Path = typer.Option(Path.cwd(), "--root", "-r", help="프로젝트 루트"),
    config_path: Optional[Path] = typer.Option(None, "--config", "-c"),
) -> None:
    """입력/override 파일 스키마 검증."""
    paths, cfg = _load(root, config_path)
    report = validate_stage.validate(paths, cfg)

    table = Table(title="Validation Report")
    table.add_column("Level")
    table.add_column("Code")
    table.add_column("Message")
    for f in report.findings:
        color = {"ERROR": "red", "WARN": "yellow", "INFO": "cyan"}.get(f.level, "white")
        table.add_row(f"[{color}]{f.level}[/{color}]", f.code, f.message)

    if report.findings:
        console.print(table)
    else:
        console.print("[green]✓ no findings[/green]")

    summary = report.summary()
    console.print(f"summary: ERROR={summary['ERROR']} WARN={summary['WARN']} INFO={summary['INFO']}")
    raise typer.Exit(code=1 if report.has_errors else 0)


@app.command()
def run(
    stage: str = typer.Option("all", "--stage", "-s", help="all|s1|s2|s3|s4|s5"),
    root: Path = typer.Option(Path.cwd(), "--root", "-r"),
    config_path: Optional[Path] = typer.Option(None, "--config", "-c"),
    tag: Optional[str] = typer.Option(None, "--tag", "-t", help="스냅샷 태그"),
) -> None:
    """단계별 실행."""
    paths, cfg = _load(root, config_path)
    logger = setup_stage_logger(stage, paths.logs_dir)

    stages_to_run = ["s1", "s2", "s3", "s4", "s5"] if stage == "all" else [stage]

    for st in stages_to_run:
        if st == "s1":
            s1_inventory.run(paths, cfg, logger)
        elif st == "s2":
            s2_metrics.run(paths, cfg, logger)
        elif st == "s3":
            s3_graph.run(paths, cfg, logger)
        elif st == "s4":
            s4_scoring.run(paths, cfg, logger)
        elif st == "s5":
            s5_report.run(paths, cfg, logger)
        else:
            console.print(f"[red]unknown stage: {st} (valid: s1, s2, s3, s4, s5, all)[/red]")
            raise typer.Exit(code=1)

    if tag:
        snap = paths.snapshot_dir(tag)
        snap.mkdir(parents=True, exist_ok=True)
        import shutil
        shutil.copytree(paths.output_dir, snap / "output", dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("_snapshots"))
        console.print(f"snapshot saved: {snap}")


@app.command()
def spike(
    root: Path = typer.Option(Path.cwd(), "--root", "-r"),
    config_path: Optional[Path] = typer.Option(None, "--config", "-c"),
) -> None:
    """§0 선행 스파이크 (SP-1/SP-2/SP-3 임계 검증)."""
    paths, cfg = _load(root, config_path)
    logger = setup_stage_logger("spike", paths.logs_dir)
    spike_stage.run(paths, cfg, logger)
    console.print(f"[green]spike report: {paths.output_dir / '_spike' / 'spike_report.md'}[/green]")


@app.command()
def diff(
    stage: str = typer.Option(..., "--stage", help="s1|s2|s3|s4"),
    from_tag: str = typer.Option(..., "--from"),
    to_tag: str = typer.Option(..., "--to"),
    root: Path = typer.Option(Path.cwd(), "--root", "-r"),
) -> None:
    """스냅샷 간 diff (§9)."""
    paths, _ = _load(root, None)
    try:
        result = diff_stage.compute_diff(paths, stage, from_tag, to_tag)
    except (ValueError, FileNotFoundError) as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(code=1)

    console.print(f"[bold]diff {stage}: {from_tag} -> {to_tag}[/bold]")
    console.print(f"[green]신규: {len(result.new_ids)}[/green]  "
                 f"[red]삭제: {len(result.removed_ids)}[/red]  "
                 f"[yellow]변경: {len(result.changed)}[/yellow]")

    if result.new_ids:
        console.print("\n[green]신규:[/green] " + ", ".join(result.new_ids))
    if result.removed_ids:
        console.print("\n[red]삭제:[/red] " + ", ".join(result.removed_ids))
    if result.changed:
        table = Table(title="변경")
        table.add_column("SP_ID")
        table.add_column("컬럼")
        table.add_column(from_tag)
        table.add_column(to_tag)
        for c in result.changed:
            table.add_row(c.key, c.column, str(c.old), str(c.new))
        console.print(table)


override_app = typer.Typer(help="override 파일 유틸")
app.add_typer(override_app, name="override")


@override_app.command("lint")
def override_lint(
    root: Path = typer.Option(Path.cwd(), "--root", "-r"),
) -> None:
    """override 정합성/충돌 검사 (§6)."""
    paths, _ = _load(root, None)
    report = override_lint_stage.lint(paths)

    table = Table(title="Override Lint Report")
    table.add_column("Level")
    table.add_column("Code")
    table.add_column("Message")
    for f in report.findings:
        color = {"ERROR": "red", "WARN": "yellow", "INFO": "cyan"}.get(f.level, "white")
        table.add_row(f"[{color}]{f.level}[/{color}]", f.code, f.message)

    if report.findings:
        console.print(table)
    else:
        console.print("[green]✓ no findings[/green]")

    raise typer.Exit(code=1 if report.has_errors else 0)


if __name__ == "__main__":
    app()
