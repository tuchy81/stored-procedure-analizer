"""S1 스모크 테스트 — examples 데이터로 end-to-end 실행."""
from __future__ import annotations

from pathlib import Path
import logging
import shutil

import pandas as pd

from sp_assessor.core.config import load_config
from sp_assessor.core.paths import ProjectPaths
from sp_assessor.stages import s1_inventory
from sp_assessor.io.csv_io import read_csv


EXAMPLES = Path(__file__).parent.parent / "examples"


def test_s1_end_to_end(tmp_path: Path) -> None:
    # arrange: copy examples/ into tmp
    for sub in ("input", "override"):
        src = EXAMPLES / sub
        if src.exists():
            shutil.copytree(src, tmp_path / sub)
    shutil.copy(EXAMPLES / "config.yaml", tmp_path / "config.yaml")

    paths = ProjectPaths.from_root(tmp_path)
    paths.ensure()
    cfg = load_config(paths.config)
    logger = logging.getLogger("test")

    # act
    result = s1_inventory.run(paths, cfg, logger)

    # assert
    assert result["inventory"] > 0
    inv = read_csv(paths.stage_output("s1_inventory") / "s1_inventory.csv")
    sp_ids = set(inv["SP_ID"])
    assert "APP_OWNER.GET_ORDER" in sp_ids
    assert "APP_OWNER.CALC_TAX" in sp_ids
    assert any(s.startswith("APP_OWNER.CUSTOMER_PKG.ADD_CUSTOMER") for s in sp_ids)
    assert any(s.startswith("APP_OWNER.CUSTOMER_PKG.GET_NAME") for s in sp_ids)

    excluded = read_csv(paths.stage_output("s1_inventory") / "s1_excluded.csv")
    ex_names = set(excluded["OBJECT_NAME"]) if "OBJECT_NAME" in excluded.columns else set()
    assert "TMP_TEST_PROC" in ex_names
    assert "LEGACY_BAK" in ex_names

    inv_row = inv[inv["SP_ID"] == "APP_OWNER.GET_ORDER"].iloc[0]
    assert int(inv_row["LOC"]) > 0

    daily = inv[inv["SP_ID"] == "BATCH_OWNER.DAILY_JOB"]
    if not daily.empty:
        assert int(daily.iloc[0]["REMOTE_REF_COUNT"]) == 1

    unresolved = read_csv(paths.stage_output("s1_inventory") / "s1_unresolved.csv")
    codes = set(unresolved["REASON_CODE"]) if "REASON_CODE" in unresolved.columns else set()
    assert "UNRESOLVED_REMOTE" in codes


def test_grant_matrix(tmp_path: Path) -> None:
    for sub in ("input", "override"):
        src = EXAMPLES / sub
        if src.exists():
            shutil.copytree(src, tmp_path / sub)
    shutil.copy(EXAMPLES / "config.yaml", tmp_path / "config.yaml")

    paths = ProjectPaths.from_root(tmp_path)
    paths.ensure()
    cfg = load_config(paths.config)
    logger = logging.getLogger("test")
    s1_inventory.run(paths, cfg, logger)

    gm = read_csv(paths.stage_output("s1_inventory") / "s1_grant_matrix.csv")
    assert not gm.empty
    assert "EXECUTE" in set(gm["PRIVILEGE"])


def _run_s1(tmp_path: Path):
    for sub in ("input", "override"):
        src = EXAMPLES / sub
        if src.exists():
            shutil.copytree(src, tmp_path / sub)
    shutil.copy(EXAMPLES / "config.yaml", tmp_path / "config.yaml")

    paths = ProjectPaths.from_root(tmp_path)
    paths.ensure()
    cfg = load_config(paths.config)
    logger = logging.getLogger("test")
    s1_inventory.run(paths, cfg, logger)
    return paths


def test_synonym_resolved_remote_unresolved(tmp_path: Path) -> None:
    """SYNC_HR depends on private synonym HR_EMP -> REMOTE_HR.EMPLOYEE@HR_LINK.
    No in_remote_objects__HR_LINK.csv is provided, so it must surface as UNRESOLVED_REMOTE."""
    paths = _run_s1(tmp_path)
    unresolved = read_csv(paths.stage_output("s1_inventory") / "s1_unresolved.csv")
    sync_hr_rows = unresolved[unresolved["SP_ID"] == "APP_OWNER.SYNC_HR"]
    assert "UNRESOLVED_REMOTE" in set(sync_hr_rows["REASON_CODE"])
    assert any("HR_EMP" in d for d in sync_hr_rows["DETAIL"])

    inv = read_csv(paths.stage_output("s1_inventory") / "s1_inventory.csv")
    sync_row = inv[inv["SP_ID"] == "APP_OWNER.SYNC_HR"].iloc[0]
    assert int(sync_row["REMOTE_REF_COUNT"]) == 0  # dependency-based, not source @link


def test_unknown_dblink_detected_from_source(tmp_path: Path) -> None:
    paths = _run_s1(tmp_path)
    unresolved = read_csv(paths.stage_output("s1_inventory") / "s1_unresolved.csv")
    direct_rows = unresolved[unresolved["SP_ID"] == "APP_OWNER.DIRECT_LINK_CALL"]
    assert "UNKNOWN_DB_LINK" in set(direct_rows["REASON_CODE"])

    inv = read_csv(paths.stage_output("s1_inventory") / "s1_inventory.csv")
    direct_row = inv[inv["SP_ID"] == "APP_OWNER.DIRECT_LINK_CALL"].iloc[0]
    assert int(direct_row["REMOTE_REF_COUNT"]) == 1  # scanned from @BOGUS_LINK in source


def test_synonym_resolves_when_remote_objects_file_present(tmp_path: Path) -> None:
    """Providing in_remote_objects__HR_LINK.csv with a matching row resolves the synonym reference."""
    for sub in ("input", "override"):
        src = EXAMPLES / sub
        if src.exists():
            shutil.copytree(src, tmp_path / sub)
    shutil.copy(EXAMPLES / "config.yaml", tmp_path / "config.yaml")

    remote_csv = tmp_path / "input" / "in_remote_objects__HR_LINK.csv"
    remote_csv.write_text(
        "OWNER,OBJECT_NAME,OBJECT_TYPE,STATUS,CREATED,LAST_DDL_TIME\n"
        "REMOTE_HR,EMPLOYEE,TABLE,VALID,2024-01-01 00:00:00,2024-01-01 00:00:00\n",
        encoding="utf-8",
    )

    paths = ProjectPaths.from_root(tmp_path)
    paths.ensure()
    cfg = load_config(paths.config)
    logger = logging.getLogger("test")
    s1_inventory.run(paths, cfg, logger)

    unresolved = read_csv(paths.stage_output("s1_inventory") / "s1_unresolved.csv")
    sync_hr_rows = unresolved[unresolved["SP_ID"] == "APP_OWNER.SYNC_HR"]
    assert sync_hr_rows.empty  # resolved via matching remote object entry

    daily_rows = unresolved[unresolved["SP_ID"] == "BATCH_OWNER.DAILY_JOB"]
    assert daily_rows.empty  # direct dependency remote ref also resolved now


def test_resolve_synonyms_private_over_public() -> None:
    from sp_assessor.stages.s1_inventory import _resolve_synonyms

    df = pd.DataFrame([
        {"OWNER": "PUBLIC", "SYNONYM_NAME": "X", "TABLE_OWNER": "PUB_OWNER", "TABLE_NAME": "T", "DB_LINK": ""},
        {"OWNER": "APP_OWNER", "SYNONYM_NAME": "X", "TABLE_OWNER": "APP_OWNER", "TABLE_NAME": "T2", "DB_LINK": ""},
    ])
    maps = _resolve_synonyms(df)
    resolved = maps.resolve("APP_OWNER", "X")
    assert resolved["TABLE_OWNER"] == "APP_OWNER" and resolved["TABLE_NAME"] == "T2"

    resolved_public = maps.resolve("OTHER_OWNER", "X")
    assert resolved_public["TABLE_OWNER"] == "PUB_OWNER"


# ---------------------------------------------------------------------------
# 회귀 테스트: pandas 의 NaN.astype(str) 동작(문자열 dtype 백엔드/버전에 따라
# "nan" 문자열로 변환되기도, NaN 그대로 남기도 함)에 의존하던 필터 버그.
# `pd.option_context('future.infer_string', False)` 로 "NaN.astype(str) == 'nan'"
# 이 되는 클래식 동작을 강제해, 설치된 pandas 버전과 무관하게 결정적으로 재현한다.
# ---------------------------------------------------------------------------

def test_decompose_packages_excludes_plain_sp_args_under_classic_pandas_dtype() -> None:
    """PACKAGE_NAME 이 NaN 인 plain SP 인자 행이 패키지 서브프로그램으로 잘못
    분류되면 _make_sp_id 가 NaN(float) 을 문자열 join 하려다 TypeError 로 죽는다."""
    from sp_assessor.stages.s1_inventory import _decompose_packages

    with pd.option_context("future.infer_string", False):
        objects = pd.DataFrame([
            {"OWNER": "APP_OWNER", "OBJECT_NAME": "GET_ORDER", "OBJECT_TYPE": "PROCEDURE", "STATUS": "VALID"},
        ])
        arguments = pd.DataFrame([
            {"OWNER": "APP_OWNER", "PACKAGE_NAME": float("nan"), "OBJECT_NAME": "GET_ORDER",
             "OVERLOAD": float("nan"), "ARGUMENT_NAME": "P_ID", "POSITION": 1,
             "DATA_TYPE": "NUMBER", "PLS_TYPE": float("nan"), "IN_OUT": "IN"},
        ])
        sources = pd.DataFrame(columns=["OWNER", "NAME", "TYPE", "LINE", "TEXT"])

        result = _decompose_packages(objects, sources, arguments)

    assert list(result["SP_ID"]) == ["APP_OWNER.GET_ORDER"]
    assert result.iloc[0]["TYPE"] == "PROCEDURE"


def test_remote_ref_count_ignores_nan_link_under_classic_pandas_dtype() -> None:
    from sp_assessor.stages.s1_inventory import _compute_remote_ref_count

    with pd.option_context("future.infer_string", False):
        inventory = pd.DataFrame([
            {"SP_ID": "APP_OWNER.GET_ORDER", "OWNER": "APP_OWNER", "PKG": "", "NAME": "GET_ORDER"},
        ])
        deps = pd.DataFrame([
            {"OWNER": "APP_OWNER", "NAME": "GET_ORDER", "REFERENCED_OWNER": "APP_OWNER",
             "REFERENCED_NAME": "ORDERS", "REFERENCED_TYPE": "TABLE", "REFERENCED_LINK_NAME": float("nan")},
        ])
        result = _compute_remote_ref_count(inventory, deps)

    assert result.iloc[0]["REMOTE_REF_COUNT"] == 0


def test_build_unresolved_ignores_nan_link_under_classic_pandas_dtype() -> None:
    from sp_assessor.stages.s1_inventory import _build_unresolved

    with pd.option_context("future.infer_string", False):
        deps = pd.DataFrame([
            {"OWNER": "APP_OWNER", "NAME": "GET_ORDER", "REFERENCED_OWNER": "APP_OWNER",
             "REFERENCED_NAME": "ORDERS", "REFERENCED_TYPE": "TABLE", "REFERENCED_LINK_NAME": float("nan")},
        ])
        objects = pd.DataFrame([
            {"OWNER": "APP_OWNER", "OBJECT_NAME": "GET_ORDER", "OBJECT_TYPE": "PROCEDURE", "STATUS": "VALID"},
        ])
        result = _build_unresolved(deps, pd.DataFrame(), objects, ["APP_OWNER"])

    assert result.empty  # 실제 DB Link 참조가 없으므로 UNRESOLVED_REMOTE 가 나오면 안 됨
