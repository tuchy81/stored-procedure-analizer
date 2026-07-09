"""디렉터리 레이아웃 및 산출물 경로."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass
class ProjectPaths:
    root: Path
    config: Path
    input_dir: Path
    bodies_dir: Path
    override_dir: Path
    output_dir: Path
    logs_dir: Path

    @classmethod
    def from_root(cls, root: str | Path) -> "ProjectPaths":
        root = Path(root)
        return cls(
            root=root,
            config=root / "config.yaml",
            input_dir=root / "input",
            bodies_dir=root / "input" / "bodies",
            override_dir=root / "override",
            output_dir=root / "output",
            logs_dir=root / "logs",
        )

    def stage_output(self, stage: str) -> Path:
        return self.output_dir / stage

    def snapshot_dir(self, tag: str) -> Path:
        return self.output_dir / "_snapshots" / tag

    def ensure(self) -> None:
        for p in (self.input_dir, self.bodies_dir, self.override_dir,
                  self.output_dir, self.logs_dir):
            p.mkdir(parents=True, exist_ok=True)
        for s in ("s1_inventory", "s2_metrics", "s3_graph", "s4_scoring", "s5_report", "_spike", "_snapshots"):
            (self.output_dir / s).mkdir(parents=True, exist_ok=True)
