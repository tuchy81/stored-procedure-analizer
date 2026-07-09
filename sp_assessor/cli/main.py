"""sp-assessor CLI entry point."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from sp_assessor.core.config import default_config, load_config
from sp_assessor.core.logging import setup_stage_logger
from sp_assessor.core.paths import ProjectPaths
from sp_assessor.stages import s1_inventory, validate as validate_stage


app = typer.Typer(add_completion=False, help="Oracle SP → Backend 전환평가 도구")
console = Console()


def _load(root: Path, config_path: Optional[Path]) -> tuple[ProjectPaths, "Config"]:
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
        else:
            console.print(f"[yellow]stage {st}: not yet implemented (phase 2+)[/yellow]")

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
) -> None:
    """§0 선행 스파이크 (Phase 6 구현 예정)."""
    console.print("[yellow]spike: not yet implemented (phase 6)[/yellow]")


@app.command()
def diff(
    stage: str = typer.Option(..., "--stage"),
    from_tag: str = typer.Option(..., "--from"),
    to_tag: str = typer.Option(..., "--to"),
    root: Path = typer.Option(Path.cwd(), "--root", "-r"),
) -> None:
    """스냅샷 간 diff (Phase 6 구현 예정)."""
    console.print("[yellow]diff: not yet implemented (phase 6)[/yellow]")


override_app = typer.Typer(help="override 파일 유틸")
app.add_typer(override_app, name="override")


@override_app.command("lint")
def override_lint(
    root: Path = typer.Option(Path.cwd(), "--root", "-r"),
) -> None:
    """override 정합성/충돌 검사 (Phase 6 구현 예정)."""
    console.print("[yellow]override lint: not yet implemented (phase 6)[/yellow]")


if __name__ == "__main__":
    app()
