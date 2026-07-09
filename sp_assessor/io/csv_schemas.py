"""입력 CSV 스키마 정의. validate 커맨드가 사용."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class CsvSchema:
    filename: str
    required_columns: tuple[str, ...]
    required: bool = True
    key_columns: tuple[str, ...] = ()


INPUT_SCHEMAS: dict[str, CsvSchema] = {
    "in_objects": CsvSchema(
        "in_objects.csv",
        ("OWNER", "OBJECT_NAME", "OBJECT_TYPE", "STATUS", "CREATED", "LAST_DDL_TIME"),
        key_columns=("OWNER", "OBJECT_NAME", "OBJECT_TYPE"),
    ),
    "in_source": CsvSchema(
        "in_source.csv",
        ("OWNER", "NAME", "TYPE", "LINE", "TEXT"),
        key_columns=("OWNER", "NAME", "TYPE", "LINE"),
    ),
    "in_dependencies": CsvSchema(
        "in_dependencies.csv",
        ("OWNER", "NAME", "TYPE", "REFERENCED_OWNER", "REFERENCED_NAME",
         "REFERENCED_TYPE", "REFERENCED_LINK_NAME", "DEPENDENCY_TYPE"),
    ),
    "in_arguments": CsvSchema(
        "in_arguments.csv",
        ("OWNER", "PACKAGE_NAME", "OBJECT_NAME", "OVERLOAD",
         "ARGUMENT_NAME", "POSITION", "DATA_TYPE", "PLS_TYPE", "IN_OUT"),
    ),
    "in_synonyms": CsvSchema(
        "in_synonyms.csv",
        ("OWNER", "SYNONYM_NAME", "TABLE_OWNER", "TABLE_NAME", "DB_LINK"),
    ),
    "in_db_links": CsvSchema(
        "in_db_links.csv",
        ("OWNER", "DB_LINK", "USERNAME", "HOST"),
    ),
    "in_tab_privs": CsvSchema(
        "in_tab_privs.csv",
        ("GRANTEE", "OWNER", "TABLE_NAME", "PRIVILEGE", "GRANTABLE", "TYPE"),
    ),
    "in_role_privs": CsvSchema(
        "in_role_privs.csv",
        ("GRANTEE", "OWNER", "TABLE_NAME", "PRIVILEGE", "DEPTH", "VIA_ROLES"),
    ),
    "in_triggers": CsvSchema(
        "in_triggers.csv",
        ("OWNER", "TRIGGER_NAME", "TABLE_OWNER", "TABLE_NAME", "STATUS", "BODY_FILE"),
    ),
    "in_scheduler_jobs": CsvSchema(
        "in_scheduler_jobs.csv",
        ("OWNER", "JOB_NAME", "JOB_TYPE", "ACTION_FILE", "ENABLED", "SCHEDULE_TEXT"),
    ),
    "in_app_calls": CsvSchema(
        "in_app_calls.csv",
        ("REPO", "FILE_PATH", "LINE_NO", "SP_NAME_RAW",
         "SP_NAME_RESOLVED", "CALL_KIND", "CALL_SNIPPET", "CONFIDENCE"),
    ),
    "in_app_constants": CsvSchema(
        "in_app_constants.csv",
        ("REPO", "FILE_PATH", "CONST_NAME", "CONST_VALUE"),
        required=False,
    ),
    "in_plscope_identifiers": CsvSchema(
        "in_plscope_identifiers.csv",
        ("OWNER", "OBJECT_NAME", "OBJECT_TYPE", "NAME", "TYPE",
         "USAGE", "USAGE_ID", "LINE", "COL", "USAGE_CONTEXT_ID"),
        required=False,
    ),
    "in_exec_stats": CsvSchema(
        "in_exec_stats.csv",
        ("OWNER", "OBJECT_NAME", "EXEC_COUNT_PERIOD"),
        required=False,
    ),
}


OVERRIDE_SCHEMAS: dict[str, CsvSchema] = {
    "s1_inventory": CsvSchema(
        "s1_inventory_override.csv",
        ("SP_ID", "ACTION", "RESOLVED_TARGET", "REASON"),
        required=False,
    ),
    "s2_metrics": CsvSchema(
        "s2_metrics_override.csv",
        ("SP_ID", "METRIC_NAME", "VALUE", "REASON"),
        required=False,
    ),
    "s2_dynsql_resolve": CsvSchema(
        "s2_dynsql_resolve.csv",
        ("SRC_SP_ID", "RESOLVED_TABLES", "REASON"),
        required=False,
    ),
    "s3_edges": CsvSchema(
        "s3_edges_override.csv",
        ("SRC", "DST", "ACTION", "REASON"),
        required=False,
    ),
    "s3_cluster": CsvSchema(
        "s3_cluster_override.csv",
        ("SP_ID", "CLUSTER_ID", "REASON"),
        required=False,
    ),
    "s4_strategy": CsvSchema(
        "s4_strategy_override.csv",
        ("SP_ID", "STRATEGY", "REASON"),
        required=False,
    ),
    "pilot_effort": CsvSchema(
        "pilot_effort.csv",
        ("SP_ID", "ACTUAL_MD"),
        required=False,
    ),
}
