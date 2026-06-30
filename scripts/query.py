#!/usr/bin/env python3
"""数据库查询工具 —— 基于预配置连接信息执行 SELECT 查询。

多环境: dev / test / prod，每个环境一个独立配置文件:
  assets/connections.dev.yaml
  assets/connections.test.yaml
  assets/connections.prod.yaml

⚠️ 安全设计: 连接配置由本脚本内部读取，AI 模型不应直接读取配置文件。
   所有数据库操作必须通过本脚本的 CLI 接口进行。
   密码通过 macOS Keychain / env / .env 解析，不存储在配置文件中。

用法:
    python query.py <db_alias> "<SQL>"                              # 默认 dev
    python query.py --env prod <db_alias> "<SQL>"                    # 切环境
    python query.py --config other.yaml <db_alias> "<SQL>"           # 自定义配置
    python query.py --list                                           # 扫描所有环境
    python query.py <db_alias> --show                                # 列出表
    python query.py <db_alias> --desc <TABLE>                    # 表结构
    python query.py <db_alias> --desc ALL                        # 全部表结构
    python query.py <db_alias> --desc "user_*"                   # 通配符匹配
    python query.py <db_alias> --ddl user_info                   # 建表 DDL
    python query.py <db_alias> --ping                                # 连接测试
    python query.py --keychain-set <alias> --env prod                # 存密码
"""

from __future__ import annotations

import argparse
import csv
import fnmatch
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# ── 路径常量 ─────────────────────────────────────────────────

SKILL_DIR = Path(__file__).resolve().parent.parent
ASSETS_DIR = SKILL_DIR / "assets"
ENV_FILE = ASSETS_DIR / ".env"
LOG_DIR = SKILL_DIR / "logs"

DEFAULT_ENV = os.environ.get("DB_QUERY_DEFAULT_ENV", "dev")


def _config_file_for(env: str) -> Path:
    """env → connections.{env}.yaml"""
    return ASSETS_DIR / f"connections.{env}.yaml"


_import_errors = []

try:
    import yaml
except ImportError:
    _import_errors.append("yaml")
    yaml = None

try:
    import json as _json_mod
except ImportError:
    _json_mod = None


# ── 密码解析 ────────────────────────────────────────────────
# 优先级: macOS Keychain > .env 文件 > 父进程环境变量
# Keychain 条目: service=db-skills/{env}/{alias}
# .env 变量名:   DB_PWD_{ENV}_{ALIAS}  (全大写，短横换下划线)

def _keychain_service(env: str, alias: str) -> str:
    return f"db-skills/{env}/{alias}"


def _dotenv_var(env: str, alias: str) -> str:
    raw = f"{env}_{alias}".upper().replace("-", "_")
    return f"DB_PWD_{raw}"


def _load_dotenv(path: Path) -> dict:
    """解析 .env 文件为 dict"""
    env = {}
    if not path.exists():
        return env
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            k, _, v = line.partition("=")
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            env[k] = v
    return env


def _resolve_password(env: str, alias: str) -> str:
    """按优先级获取密码"""
    service = _keychain_service(env, alias)
    env_var = _dotenv_var(env, alias)

    # 1 — macOS Keychain
    if sys.platform == "darwin":
        try:
            result = subprocess.run(
                [
                    "security", "find-generic-password",
                    "-a", "db-skills",
                    "-s", service,
                    "-w",
                ],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

    # 2 — .env 文件
    dotenv = _load_dotenv(ENV_FILE)
    if env_var in dotenv:
        return dotenv[env_var]

    # 3 — 父进程环境变量
    if env_var in os.environ:
        return os.environ[env_var]

    raise RuntimeError(
        f"无法获取 [{alias}] ({env} 环境) 的密码。请通过以下任一方式配置:\n"
        f"  1. Keychain: security add-generic-password -a db-skills -s {service} -w '密码'\n"
        f"  2. assets/.env: {env_var}=密码\n"
        f"  3. 环境变量: export {env_var}=密码"
    )


# ── 环境配置加载 ────────────────────────────────────────────

def _resolve_placeholders(data):
    """递归替换数据中所有字符串值里的 ${VAR} 占位符。

    查找顺序: .env 文件 → 父进程环境变量
    未找到的占位符保持原样并打印警告。
    """
    dotenv = _load_dotenv(ENV_FILE)

    def _replace(val):
        if not isinstance(val, str):
            return val

        def _lookup(match):
            var = match.group(1)
            if var in dotenv:
                return dotenv[var]
            if var in os.environ:
                return os.environ[var]
            print(
                f"[WARN] 占位符 ${{{var}}} 在 .env 和环境变量中均未找到，保持原样",
                file=sys.stderr,
            )
            return match.group(0)
        return re.sub(r"\$\{(\w+)\}", _lookup, val)

    return _walk_replace(data, _replace)


def _walk_replace(data, fn):
    """递归遍历 dict/list，对每个字符串值应用 fn"""
    if isinstance(data, dict):
        return {k: _walk_replace(v, fn) for k, v in data.items()}
    elif isinstance(data, list):
        return [_walk_replace(v, fn) for v in data]
    else:
        return fn(data)


def _load_yaml_file(path: Path) -> dict:
    if yaml is None:
        raise ImportError(
            "需要 PyYAML 来读取 YAML 配置。请安装: pip install pyyaml"
        )
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _load_json_file(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return _json_mod.load(f) or {}


def _load_any_config(path: Path) -> dict:
    """加载 YAML 或 JSON 配置文件"""
    if not path.exists():
        raise FileNotFoundError(f"配置文件不存在: {path}")
    suffix = path.suffix.lower()
    if suffix in (".yaml", ".yml"):
        return _load_yaml_file(path)
    elif suffix == ".json":
        return _load_json_file(path)
    raise ValueError(f"不支持的配置格式: {suffix}")


def load_env_config(env: str, config_override: str | None = None) -> dict:
    """加载环境的连接配置，返回 connections 字典。

    1. --config 指定 → 加载该文件
    2. --env dev|test|prod → connections.{env}.yaml

    加载后会对所有值进行 ${VAR} 占位符替换，优先查 .env 文件，其次查环境变量。
    """
    if config_override:
        path = Path(config_override)
        if not path.is_absolute():
            path = ASSETS_DIR / path
    else:
        path = _config_file_for(env)

    if not path.exists():
        if config_override:
            raise FileNotFoundError(f"配置文件不存在: {path}")
        raise FileNotFoundError(
            f"环境 [{env}] 配置文件不存在: {path.name}\n"
            f"请从 assets/{path.stem}.example 复制并编辑。"
        )
    config = _load_any_config(path)
    connections = config.get("connections", {})
    return _resolve_placeholders(connections)


# ── 连接获取 ────────────────────────────────────────────────

def get_connection(db_alias: str, env: str, config_override: str | None = None) -> dict:
    """获取单个数据库连接信息（含密码注入）"""
    connections = load_env_config(env, config_override)
    if db_alias not in connections:
        available = list(connections.keys())
        raise ValueError(
            f"未找到数据库别名 [{db_alias}] ({env} 环境)\n"
            f"可用别名: {', '.join(available) if available else '(无)'}"
        )
    conn = dict(connections[db_alias])  # 浅拷贝，避免污染缓存
    db_type = conn.get("type", "").lower()
    if db_type in ("sqlite", "sqlite3"):
        # SQLite 只需要 type + path，默认 :memory:
        conn.setdefault("path", ":memory:")
    else:
        required = ["type", "host", "port", "user", "database"]
        missing = [k for k in required if k not in conn]
        if missing:
            raise ValueError(
                f"数据库 {db_alias} 配置不完整，缺少字段: {', '.join(missing)}"
            )
        # 密码: YAML 中已解析的 ${VAR} 优先，否则走约定查找
        if not conn.get("password"):
            conn["password"] = _resolve_password(env, db_alias)
    return conn


# ── 数据库驱动 ──────────────────────────────────────────────

def _get_mysql_connection(conn_info: dict, timeout: int | None = None):
    try:
        import pymysql
    except ImportError:
        raise ImportError("需要 pymysql。请安装: pip install pymysql")
    kwargs = dict(
        host=conn_info["host"],
        port=conn_info.get("port", 3306),
        user=conn_info["user"],
        password=conn_info.get("password", ""),
        database=conn_info["database"],
        charset=conn_info.get("charset", "utf8mb4"),
        connect_timeout=conn_info.get("connect_timeout", 10),
        autocommit=True,   # CLI 工具每条语句自动提交
    )
    if timeout:
        kwargs["read_timeout"] = timeout
    return pymysql.connect(**kwargs)


def _get_pg_connection(conn_info: dict, timeout: int | None = None):
    try:
        import psycopg2
    except ImportError:
        raise ImportError("需要 psycopg2。请安装: pip install psycopg2-binary")
    kwargs = dict(
        host=conn_info["host"],
        port=conn_info.get("port", 5432),
        user=conn_info["user"],
        password=conn_info.get("password", ""),
        dbname=conn_info["database"],
        connect_timeout=conn_info.get("connect_timeout", 10),
    )
    conn = psycopg2.connect(**kwargs)
    conn.autocommit = True
    if timeout:
        kwargs["options"] = f"-c statement_timeout={timeout * 1000}"
    return psycopg2.connect(**kwargs)


def _get_sqlite_connection(conn_info: dict, timeout: int | None = None):
    """SQLite 驱动（使用标准库 sqlite3，零额外依赖）。
    支持 path 字段: ':memory:' 或文件路径。
    """
    import sqlite3
    path = conn_info.get("path", ":memory:")
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.isolation_level = None  # 真正的 autocommit
    if timeout:
        conn.execute(f"PRAGMA busy_timeout = {timeout * 1000}")
    return conn


_DRIVER_MAP = {
    "mysql": _get_mysql_connection,
    "mariadb": _get_mysql_connection,
    "postgresql": _get_pg_connection,
    "postgres": _get_pg_connection,
    "sqlite": _get_sqlite_connection,
    "sqlite3": _get_sqlite_connection,
}


def _open_raw_connection(db_alias: str, env: str,
                         config_override: str | None = None,
                         timeout: int | None = None):
    """打开一条原始数据库连接，返回 (conn, db_type)。调用方负责关闭。"""
    conn_info = get_connection(db_alias, env, config_override)
    db_type = conn_info["type"].lower()
    if db_type not in _DRIVER_MAP:
        raise ValueError(
            f"不支持的数据库类型: {db_type}\n"
            f"支持: {', '.join(_DRIVER_MAP)}"
        )
    return _DRIVER_MAP[db_type](conn_info, timeout=timeout), db_type


# ── 查询日志 ────────────────────────────────────────────────

def _log_query(db_alias: str, env: str, sql: str, row_count: int,
               elapsed: float, status: str = "OK", op_type: str = ""):
    """将查询记录写入日志文件。日志位于 db-skills/logs/YYYY-MM-DD.log。"""
    try:
        if not LOG_DIR.exists():
            LOG_DIR.mkdir(parents=True, exist_ok=True)
        today = datetime.now().strftime("%Y-%m-%d")
        log_file = LOG_DIR / f"{today}.log"
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        tag = f" {op_type}" if op_type else ""
        line = (
            f"[{timestamp}] {env}:{db_alias} |{tag} "
            f"{sql} | "
            f"{row_count} rows | "
            f"{elapsed:.3f}s | "
            f"{status}\n"
        )
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass


# ── 查询执行 ────────────────────────────────────────────────

def execute_query(db_alias: str, sql: str, env: str,
                  config_override: str | None = None,
                  _conn=None, _timeout: int | None = None,
                  _op_type: str = ""):
    """执行 SQL。返回:
      - 如果是 SELECT: (columns, rows)
      - 如果是 DML/DDL: ([], affected_rows)
    """
    own_conn = False
    if _conn is not None:
        conn = _conn
    else:
        if _timeout is not None:
            conn, _ = _open_raw_connection(db_alias, env, config_override, timeout=_timeout)
        else:
            conn, _ = _open_raw_connection(db_alias, env, config_override)
        own_conn = True
    try:
        cursor = conn.cursor()
        start = time.time()
        cursor.execute(sql)
        elapsed = time.time() - start

        # DML/DDL vs SELECT 分叉
        if cursor.description:
            columns = [desc[0] for desc in cursor.description]
            rows = cursor.fetchall()
            _log_query(db_alias, env, sql.strip(), len(rows), elapsed,
                       op_type=_op_type)
            return columns, rows
        else:
            affected = cursor.rowcount
            _log_query(db_alias, env, sql.strip(), affected, elapsed,
                       op_type=_op_type or "WRITE")
            return [], affected
    except Exception:
        elapsed = time.time() - start
        _log_query(db_alias, env, sql.strip(), 0, elapsed, "ERROR", _op_type)
        raise
    finally:
        if own_conn:
            conn.close()


def list_tables(db_alias: str, env: str, config_override: str | None = None,
                _conn=None):
    conn_info = get_connection(db_alias, env, config_override)
    db_type = conn_info["type"].lower()
    if db_type in ("mysql", "mariadb"):
        sql = "SHOW TABLES"
    elif db_type in ("postgresql", "postgres"):
        sql = (
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' ORDER BY table_name"
        )
    elif db_type in ("sqlite", "sqlite3"):
        sql = "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    else:
        raise ValueError(f"不支持的表列表查询: {db_type}")
    columns, rows = execute_query(db_alias, sql, env, config_override, _conn=_conn)
    return [r[0] for r in rows]


def list_tables_with_info(db_alias: str, env: str,
                          config_override: str | None = None,
                          _conn=None) -> tuple:
    """获取表列表（含 COMMENT + 预估行数）。

    返回: (columns, rows)
      columns: ["Table", "Rows", "Comment"]
      rows:    [(name, row_count, comment), ...]

    MySQL: information_schema.TABLES
    PostgreSQL: information_schema.tables + pg_class
    """
    conn_info = get_connection(db_alias, env, config_override)
    db_type = conn_info["type"].lower()

    if db_type in ("mysql", "mariadb"):
        sql = (
            "SELECT TABLE_NAME, TABLE_ROWS, TABLE_COMMENT "
            "FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA = DATABASE() "
            "ORDER BY TABLE_NAME"
        )
    elif db_type in ("postgresql", "postgres"):
        sql = (
            "SELECT "
            "  t.table_name, "
            "  COALESCE(c.reltuples::bigint, 0), "
            "  pg_catalog.obj_description(c.oid) "
            "FROM information_schema.tables t "
            "LEFT JOIN pg_class c ON c.relname = t.table_name "
            "WHERE t.table_schema = 'public' "
            "ORDER BY t.table_name"
        )
    elif db_type in ("sqlite", "sqlite3"):
        # SQLite: 表名 + 行数估算，无 COMMENT
        sql = (
            "SELECT name, 0, '' FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    else:
        raise ValueError(f"不支持的数据库类型: {db_type}")

    columns, rows = execute_query(db_alias, sql, env, config_override, _conn=_conn)
    columns = ["Table", "Rows", "Comment"]
    return columns, rows


def show_create_table(db_alias: str, table_name: str, env: str,
                     config_override: str | None = None,
                     _conn=None) -> str:
    """获取表的 CREATE TABLE DDL 语句。

    MySQL/MariaDB: 使用 SHOW CREATE TABLE。
    PostgreSQL: 从 information_schema + pg_indexes 重组成 DDL 格式。
    返回完整的 DDL 文本。
    """
    conn_info = get_connection(db_alias, env, config_override)
    db_type = conn_info["type"].lower()

    if db_type in ("mysql", "mariadb"):
        sql = f"SHOW CREATE TABLE {table_name}"
        columns, rows = execute_query(db_alias, sql, env, config_override, _conn=_conn)
        if rows and len(rows[0]) >= 2:
            return rows[0][1]  # 第二列是 Create Table
        return f"-- 无法获取 {table_name} 的 DDL"

    elif db_type in ("postgresql", "postgres"):
        # 从 information_schema 获取列信息
        col_sql = (
            "SELECT "
            "  c.column_name, "
            "  c.data_type, "
            "  c.character_maximum_length, "
            "  c.is_nullable, "
            "  c.column_default, "
            "  pg_catalog.col_description(c.table_name::regclass, c.ordinal_position) AS comment "
            "FROM information_schema.columns c "
            f"WHERE c.table_name = '{table_name}' "
            "ORDER BY c.ordinal_position"
        )
        col_cols, col_rows = execute_query(db_alias, col_sql, env, config_override, _conn=_conn)

        # 获取索引信息
        idx_sql = (
            "SELECT indexname, indexdef FROM pg_indexes "
            f"WHERE tablename = '{table_name}' ORDER BY indexname"
        )
        _, idx_rows = execute_query(db_alias, idx_sql, env, config_override, _conn=_conn)

        lines = [f"CREATE TABLE {table_name} ("]
        for r in col_rows:
            name = r[0]
            dtype = r[1]
            maxlen = r[2]
            nullable = r[3]
            default = r[4]
            comment = r[5]

            if dtype in ("character varying", "character", "varchar") and maxlen:
                dtype = f"varchar({maxlen})"

            col_def = f"  {name} {dtype}"
            if nullable == "NO":
                col_def += " NOT NULL"
            if default:
                col_def += f" DEFAULT {default}"
            lines.append(col_def + ",")

        lines.append(");")
        ddl = "\n".join(lines)

        # 追加索引
        if idx_rows:
            ddl += "\n"
            for idx in idx_rows:
                ddl += f"\n{idx[1]};"

        return ddl

    elif db_type in ("sqlite", "sqlite3"):
        sql = "SELECT sql FROM sqlite_master WHERE type='table' AND name=?"
        params_sql = sql.replace("?", f"'{table_name}'")
        columns, rows = execute_query(db_alias, params_sql, env, config_override, _conn=_conn)
        if rows and rows[0][0]:
            return rows[0][0]
        return f"-- 无法获取 {table_name} 的 DDL（可能是虚拟表或系统表）"

    else:
        raise ValueError(f"不支持的表结构查询: {db_type}")


def describe_table(db_alias: str, table_name: str, env: str,
                  config_override: str | None = None,
                  _conn=None):
    """获取完整表结构 (列定义 + 注释) —— 保留作为内部 API"""
    conn_info = get_connection(db_alias, env, config_override)
    db_type = conn_info["type"].lower()
    if db_type in ("mysql", "mariadb"):
        sql = f"SHOW FULL COLUMNS FROM {table_name}"
        columns, rows = execute_query(db_alias, sql, env, config_override, _conn=_conn)
        skip = {"Collation", "Privileges"}
        idx_map = [i for i, c in enumerate(columns) if c not in skip]
        columns = [columns[i] for i in idx_map]
        rows = [tuple(r[i] for i in idx_map) for r in rows]
    elif db_type in ("postgresql", "postgres"):
        sql = (
            "SELECT "
            "  c.column_name AS Field, "
            "  c.data_type AS Type, "
            "  c.is_nullable AS Null, "
            "  c.column_default AS Default, "
            "  pg_catalog.col_description(c.table_name::regclass, c.ordinal_position) AS Comment "
            "FROM information_schema.columns c "
            f"WHERE c.table_name = '{table_name}' "
            "ORDER BY c.ordinal_position"
        )
        columns, rows = execute_query(db_alias, sql, env, config_override, _conn=_conn)
    elif db_type in ("sqlite", "sqlite3"):
        sql = f"PRAGMA table_info({table_name})"
        columns, rows = execute_query(db_alias, sql, env, config_override, _conn=_conn)
        # cid / name / type / notnull / dflt_value / pk
        columns = ["Field", "Type", "Null", "Default", "Key", "Extra", "Comment"]
        rows = [
            (r[1], r[2], "NO" if r[3] else "YES", r[4] or "",
             "PRI" if r[5] else "", "",
             "")
            for r in rows
        ]
    else:
        raise ValueError(f"不支持的表结构查询: {db_type}")
    return columns, rows


def get_table_comment(db_alias: str, table_name: str, env: str,
                      config_override: str | None = None,
                      _conn=None) -> str:
    """获取表 COMMENT 信息。MySQL 从 SHOW TABLE STATUS 取，PG 从 pg_description 取。"""
    try:
        conn_info = get_connection(db_alias, env, config_override)
        db_type = conn_info["type"].lower()
        if db_type in ("mysql", "mariadb"):
            sql = f"SHOW TABLE STATUS WHERE Name = '{table_name}'"
            columns, rows = execute_query(db_alias, sql, env, config_override, _conn=_conn)
            if rows and "Comment" in columns:
                idx = columns.index("Comment")
                comment = rows[0][idx]
                return comment if comment else ""
        elif db_type in ("postgresql", "postgres"):
            sql = (
                "SELECT obj_description(c.oid) "
                "FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                f"WHERE c.relname = '{table_name}' AND n.nspname = 'public'"
            )
            _, rows = execute_query(db_alias, sql, env, config_override, _conn=_conn)
            if rows and rows[0][0]:
                return rows[0][0]
    except (FileNotFoundError, ImportError, RuntimeError, OSError):
        # 连接失败 / 驱动缺失 / 配置错误 —— 静默返回空
        pass
    return ""


def describe_indexes(db_alias: str, table_name: str, env: str,
                     config_override: str | None = None,
                     _conn=None):
    """获取表索引信息 —— 保留作为内部 API"""
    conn_info = get_connection(db_alias, env, config_override)
    db_type = conn_info["type"].lower()
    if db_type in ("mysql", "mariadb"):
        sql = f"SHOW INDEX FROM {table_name}"
        columns, rows = execute_query(db_alias, sql, env, config_override, _conn=_conn)
        keep = {"Non_unique", "Key_name", "Seq_in_index", "Column_name",
                "Null", "Index_type", "Comment"}
        idx_map = [i for i, c in enumerate(columns) if c in keep]
        columns = [columns[i] for i in idx_map]
        rows = [tuple(r[i] for i in idx_map) for r in rows]
        return columns, rows
    elif db_type in ("postgresql", "postgres"):
        sql = (
            "SELECT "
            "  indexname AS Key_name, "
            "  indexdef AS Index_def "
            "FROM pg_indexes "
            f"WHERE tablename = '{table_name}' "
            "ORDER BY indexname"
        )
    elif db_type in ("sqlite", "sqlite3"):
        sql = f"PRAGMA index_list({table_name})"
        columns, rows = execute_query(db_alias, sql, env, config_override, _conn=_conn)
        # seq / name / unique / origin / partial
        columns = ["Non_unique", "Key_name", "Column_name", "Index_type"]
        result_rows = []
        for r in rows:
            idx_name = r[1]
            is_unique = r[2]
            # 获取索引列
            info_sql = f"PRAGMA index_info({idx_name})"
            _, info_rows = execute_query(db_alias, info_sql, env, config_override, _conn=_conn)
            for ir in info_rows:
                result_rows.append((0 if is_unique else 1, idx_name, ir[2], "btree"))
        return columns, result_rows
    else:
        raise ValueError(f"不支持的数据库类型: {db_type}")
    columns, rows = execute_query(db_alias, sql, env, config_override, _conn=_conn)
    return columns, rows


def list_connections(env: str | None = None,
                     config_override: str | None = None):
    """列出所有已配置的数据库别名。

    扫描 assets/connections.*.yaml，按环境分组显示。
    如果指定 env，只列该环境。
    """
    all_envs = []

    if config_override:
        path = Path(config_override)
        if not path.is_absolute():
            path = ASSETS_DIR / path
        if path.exists():
            config = _load_any_config(path)
            conns = config.get("connections", {})
            if conns:
                all_envs.append((path.stem, conns))
    else:
        for f in sorted(ASSETS_DIR.glob("connections.*.yaml")):
            env_name = f.stem.replace("connections.", "")
            if env and env_name != env:
                continue
            try:
                config = _load_any_config(f)
                conns = config.get("connections", {})
                if conns:
                    all_envs.append((env_name, conns))
            except Exception:
                continue

    if not all_envs:
        print("(无已配置的数据库连接)")
        return

    header = f"{'环境':<10} {'别名':<22} {'类型':<12} {'主机':<20} {'端口':<8} {'数据库'}"
    print(f"默认环境: {DEFAULT_ENV}\n")
    print(header)
    print("-" * 82)
    for env_name, conns in all_envs:
        for alias, info in conns.items():
            db_type = info.get("type", "unknown")
            host = info.get("host", "-")
            port = str(info.get("port", "-"))
            db_name = info.get("database", "-")
            mark = " *" if env_name == DEFAULT_ENV else ""
            print(f"{env_name + mark:<10} {alias:<22} {db_type:<12} {host:<20} {port:<8} {db_name}")


# ── 格式化输出 ──────────────────────────────────────────────

def format_output(columns, rows, fmt="table", show_row_count=True):
    if fmt == "json":
        result = [dict(zip(columns, row)) for row in rows]
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    elif fmt == "csv":
        writer = csv.writer(sys.stdout)
        writer.writerow(columns)
        writer.writerows(rows)
    else:
        if not columns:
            print("(查询无返回列)")
            return
        col_widths = [len(c) for c in columns]
        for row in rows:
            for i, val in enumerate(row):
                col_widths[i] = max(col_widths[i], len(str(val)) if val is not None else 4)
        header = " | ".join(c.ljust(col_widths[i]) for i, c in enumerate(columns))
        sep = "-+-".join("-" * col_widths[i] for i in range(len(columns)))
        print(header)
        print(sep)
        for row in rows:
            line = " | ".join(
                (str(v) if v is not None else "NULL").ljust(col_widths[i])
                for i, v in enumerate(row)
            )
            print(line)
        if show_row_count:
            print(f"\n({len(rows)} 行)")


# ── SQL 分析与保护 ──────────────────────────────────────────

DEFAULT_LIMIT = 100
LARGE_TABLE_THRESHOLD = 50000


def _has_limit(sql: str) -> bool:
    cleaned = re.sub(r";\s*$", "", sql.strip())
    return bool(re.search(r"\bLIMIT\s+\d+\s*$", cleaned, re.IGNORECASE))


def _inject_limit(sql: str, limit: int, db_type: str = "mysql") -> str:
    """给 SQL 注入 LIMIT。支持 SELECT / DELETE / UPDATE（INSERT 不适用）。
    PostgreSQL 不支持 DELETE LIMIT，会用 CTE 子查询替代。
    """
    cleaned = re.sub(r";\s*$", "", sql.strip())
    upper = cleaned.upper()

    if upper.startswith("DELETE") and db_type in ("postgresql", "postgres"):
        # PG: DELETE FROM t WHERE ctid IN (SELECT ctid FROM t WHERE ... LIMIT N)
        # 提取表名和 WHERE 条件
        m = re.match(
            r"DELETE\s+FROM\s+(\w+)\s+(WHERE\s+.+)", cleaned,
            re.IGNORECASE,
        )
        if m:
            table = m.group(1)
            where = m.group(2)
            return (
                f"DELETE FROM {table} "
                f"WHERE ctid IN (SELECT ctid FROM {table} {where} LIMIT {limit})"
            )
        # 无 WHERE → 不改（_check_where_clause 会拦截）

    return f"{cleaned} LIMIT {limit}"


def _get_explain_info(db_alias: str, sql: str, env: str,
                      config_override: str | None = None):
    """运行 EXPLAIN，返回 (预估行数, 索引摘要字符串)。

    索引摘要格式: "type=ref | key=idx_mobile | rows=42"
    全表扫描时会标记: "type=ALL (全表扫描)"
    """
    conn_info = get_connection(db_alias, env, config_override)
    db_type = conn_info["type"].lower()
    driver_fn = _DRIVER_MAP[db_type]
    conn = driver_fn(conn_info)
    t0 = time.time()
    try:
        cursor = conn.cursor()
        if db_type in ("mysql", "mariadb"):
            explain_sql = f"EXPLAIN {sql}"
            cursor.execute(explain_sql)
            rows = cursor.fetchall()
            elapsed = time.time() - t0
            _log_query(db_alias, env, explain_sql, len(rows), elapsed)
            if not rows:
                return None, ""
            cols = [desc[0].lower() for desc in cursor.description] if cursor.description else []

            # 汇总预估行数
            try:
                row_idx = cols.index("rows")
                estimated = sum(int(r[row_idx] or 0) for r in rows)
            except (ValueError, IndexError):
                estimated = None

            # 生成索引用法摘要
            parts = []
            # type: 访问类型
            try:
                t = rows[0][cols.index("type")]
                if t:
                    label = str(t)
                    if str(t).upper() == "ALL":
                        label = "ALL (全表扫描)"
                    parts.append(f"type={label}")
            except (ValueError, IndexError):
                pass
            # key: 使用的索引
            try:
                k = rows[0][cols.index("key")]
                if k:
                    parts.append(f"key={k}")
                else:
                    parts.append("key=NULL")
            except (ValueError, IndexError):
                pass
            # rows: 预估扫描行数
            try:
                r = rows[0][cols.index("rows")]
                parts.append(f"rows={_format_number(int(r or 0))}")
            except (ValueError, IndexError):
                pass
            # Extra: 额外信息（只截取关键部分）
            try:
                extra = str(rows[0][cols.index("extra")] or "")
                if "Using filesort" in extra:
                    parts.append("Using filesort")
                if "Using temporary" in extra:
                    parts.append("Using temporary")
                if "Using where" in extra and "Using index" in extra:
                    pass  # 覆盖索引场景不额外标记，本身是好事
            except (ValueError, IndexError):
                pass

            summary = " | ".join(parts)
            return estimated, summary

        elif db_type in ("postgresql", "postgres"):
            explain_sql = f"EXPLAIN (FORMAT JSON) {sql}"
            cursor.execute(explain_sql)
            result = cursor.fetchone()
            elapsed = time.time() - t0
            _log_query(db_alias, env, explain_sql, 1 if result else 0, elapsed)
            if result and result[0]:
                plan = result[0][0].get("Plan", {})
                estimated = int(plan.get("Plan Rows", 0))
                # PG 摘要
                node_type = plan.get("Node Type", "")
                index_name = plan.get("Index Name", plan.get("Relation Name", ""))
                scan = plan.get("Index Cond", plan.get("Filter", ""))
                part_str = f"type={node_type}"
                if index_name:
                    part_str += f" | key={index_name}"
                if scan:
                    # 截断过长的条件
                    part_str += f" | cond={str(scan)[:40]}"
                return estimated, part_str

        elif db_type in ("sqlite", "sqlite3"):
            explain_sql = f"EXPLAIN QUERY PLAN {sql}"
            cursor.execute(explain_sql)
            rows = cursor.fetchall()
            elapsed = time.time() - t0
            _log_query(db_alias, env, explain_sql, len(rows), elapsed)
            if not rows:
                return None, ""
            # SQLite EXPLAIN QUERY PLAN: id|parent|notused|detail
            details = [r[3] for r in rows if len(r) > 3 and r[3]]
            summary = " | ".join(details[:3])
            return None, summary

        elapsed = time.time() - t0
        _log_query(db_alias, env, sql, 0, elapsed, "EMPTY")
        return None, ""
    except Exception:
        elapsed = time.time() - t0
        _log_query(db_alias, env, f"EXPLAIN {sql}", 0, elapsed, "ERROR")
        return None, ""


def _get_estimated_rows(db_alias: str, sql: str, env: str,
                        config_override: str | None = None):
    """兼容旧接口，只返回预估行数。"""
    est, _summary = _get_explain_info(db_alias, sql, env, config_override)
    return est


def _execute_count(db_alias: str, sql: str, env: str,
                   config_override: str | None = None):
    count_sql = re.sub(
        r"\bORDER\s+BY\s+.+?(\bLIMIT\b|$)", "", sql,
        flags=re.IGNORECASE | re.DOTALL,
    )
    count_sql = re.sub(r"\bLIMIT\s+\d+", "", count_sql, flags=re.IGNORECASE)
    count_sql = re.sub(
        r"SELECT\s+.+?\s+FROM", "SELECT COUNT(*) FROM",
        count_sql, count=1,
        flags=re.IGNORECASE | re.DOTALL,
    )
    columns, rows = execute_query(db_alias, count_sql, env, config_override)
    if rows and len(rows) > 0:
        return int(rows[0][0])
    return 0


def _format_number(n: int) -> str:
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    elif n >= 1_000:
        return f"{n/1_000:.1f}K"
    return str(n)


# ── SQL 分类 ────────────────────────────────────────────────

# 操作分级:
#   READ   — SELECT / SHOW / DESCRIBE / EXPLAIN（始终允许）
#   DML    — INSERT / UPDATE / DELETE / REPLACE（需配置 readonly: false）
#   DDL    — ALTER / CREATE / DROP / TRUNCATE / RENAME（需配置 allow_ddl: true）
#   BLOCKED— CALL / EXECUTE / PREPARE / GRANT / REVOKE / LOCK（始终拒绝）

_READ_PREFIXES = ("SELECT", "SHOW", "DESCRIBE", "EXPLAIN", "DESC ")
_DML_PREFIXES = ("INSERT", "UPDATE", "DELETE", "REPLACE")
_DDL_PREFIXES = ("ALTER", "CREATE", "DROP", "TRUNCATE", "RENAME")
_BLOCKED_PREFIXES = ("CALL", "EXECUTE", "EXEC", "PREPARE", "GRANT",
                     "REVOKE", "LOCK", "UNLOCK", "FLUSH", "KILL",
                     "RESET", "SET ", "HANDLER", "LOAD ")


def _sql_type(stripped_sql: str) -> str:
    """返回 SQL 操作级别: READ / DML / DDL / BLOCKED"""
    s = stripped_sql.upper()  # 防御性大写
    for p in _BLOCKED_PREFIXES:
        if s.startswith(p):
            return "BLOCKED"
    for p in _DDL_PREFIXES:
        if s.startswith(p):
            return "DDL"
    for p in _DML_PREFIXES:
        if s.startswith(p):
            return "DML"
    for p in _READ_PREFIXES:
        if s.startswith(p):
            return "READ"
    return "BLOCKED"  # 无法识别的也拒绝


def _strip_sql_comments(sql: str) -> str:
    """去掉 SQL 前缀的注释，返回大写开头的纯净 SQL"""
    stripped = sql.strip().upper()
    while stripped.startswith("--") or stripped.startswith("/*"):
        if stripped.startswith("--"):
            nl = stripped.find("\n")
            stripped = stripped[nl + 1:].strip() if nl != -1 else ""
        elif stripped.startswith("/*"):
            end = stripped.find("*/")
            stripped = stripped[end + 2:].strip() if end != -1 else ""
        else:
            break
    return stripped


def _resolve_write_permission(db_alias: str, env: str,
                              config_override: str | None = None) -> tuple:
    """解析写权限。返回 (allow_dml: bool, allow_ddl: bool)。

    优先级: 连接级 > 环境级 > 全局默认(禁用)
    """
    # 加载原始配置（需要 top-level settings）
    if config_override:
        path = Path(config_override)
        if not path.is_absolute():
            path = ASSETS_DIR / path
    else:
        path = _config_file_for(env)

    settings: dict = {}
    conn: dict = {}
    try:
        if path.exists():
            raw = _load_any_config(path)
            settings = raw.get("settings", {})
            if isinstance(settings, dict):
                pass
            else:
                settings = {}
            connections = raw.get("connections", {})
            conn = connections.get(db_alias, {})
            if not isinstance(conn, dict):
                conn = {}
    except (FileNotFoundError, ImportError, RuntimeError, OSError):
        pass

    global_readonly: bool | None = settings.get("readonly_mode") if isinstance(settings, dict) else None
    global_allow_ddl: bool = settings.get("allow_ddl", False) if isinstance(settings, dict) else False

    conn_readonly: bool | None = conn.get("readonly") if isinstance(conn, dict) else None
    conn_allow_ddl: bool | None = conn.get("allow_ddl") if isinstance(conn, dict) else None

    # 连接级覆盖环境级
    if conn_readonly is not None:
        readonly = conn_readonly
    elif global_readonly is not None:
        readonly = global_readonly
    else:
        readonly = True  # 默认只读

    if conn_allow_ddl is not None:
        allow_ddl = conn_allow_ddl
    elif global_allow_ddl is not None:
        allow_ddl = global_allow_ddl
    else:
        allow_ddl = False  # 默认禁止 DDL

    return (not readonly), bool(allow_ddl)


def _check_where_clause(sql: str, op_type: str):
    """检查 DELETE/UPDATE 是否有 WHERE 子句。无 WHERE 时拒绝。"""
    if op_type != "DML":
        return
    upper = sql.upper()
    if upper.startswith("DELETE") or upper.startswith("UPDATE"):
        if " WHERE " not in upper:
            raise ValueError(
                f"拒绝无 WHERE 的 {upper.split()[0]} 操作，这会修改整表数据。"
                f"\n如确认需要全表操作，请添加 WHERE 1=1。"
            )


def _confirm_write(env: str, db_alias: str, conn_info: dict,
                   sql: str, op_type: str):
    """写操作确认提示。prod 环境强制确认，非交互式通过环境变量跳过。"""
    assume_yes = os.environ.get("DB_QUERY_ASSUME_YES", "").strip() in ("1", "yes", "true")

    label = "DDL" if op_type == "DDL" else "DML"
    host_info = conn_info.get("host", conn_info.get("path", "?"))
    database = conn_info.get("database", conn_info.get("path", "?"))

    msg = (
        f"\n⚠️  即将执行 {label} 操作:\n"
        f"    环境: {env}\n"
        f"    连接: {db_alias} ({host_info}/{database})\n"
        f"    SQL:  {sql[:200]}\n"
    )

    if env == "prod":
        # prod 环境一律强制确认
        if assume_yes:
            print(msg + "    确认: 已跳过 (DB_QUERY_ASSUME_YES=1)")
            return
        print(msg + "\n    [prod 环境] 输入 yes 确认执行: ", end="", flush=True)
        try:
            answer = sys.stdin.readline().strip()
        except (EOFError, KeyboardInterrupt):
            raise RuntimeError("已取消写操作")
        if answer.lower() != "yes":
            raise RuntimeError("已取消写操作")
    elif assume_yes:
        print(msg + "    确认: 已跳过 (DB_QUERY_ASSUME_YES=1)")
    else:
        # 非 prod 环境，直接执行但打印提示
        print(msg + "    确认: 非 prod 环境，自动确认执行")


def validate_sql(sql: str, db_alias: str | None = None, env: str | None = None,
                 config_override: str | None = None, dry_run: bool = False):
    """校验 SQL 操作级别，根据配置放行或拒绝。

    返回: (op_type: str)  — "READ" / "DML" / "DDL"
    抛出: ValueError 如果操作被拒绝
    """
    stripped = _strip_sql_comments(sql)
    op_type = _sql_type(stripped)

    if op_type == "BLOCKED":
        raise ValueError(
            f"拒绝执行此语句类型，收到: {sql[:50]}...\n"
            f"支持: SELECT/SHOW/DESCRIBE/EXPLAIN/INSERT/UPDATE/DELETE/REPLACE\n"
            f"受控: ALTER/CREATE/DROP/TRUNCATE (需 allow_ddl: true)"
        )

    if op_type == "READ":
        return op_type

    # DML / DDL — 检查配置权限
    if db_alias is None or env is None:
        raise ValueError(
            f"{'DDL' if op_type == 'DDL' else 'DML'} 操作被拒绝：写操作未启用。"
            f"\n请在 YAML 中设置 readonly: false (DML) 或 allow_ddl: true (DDL)。"
        )

    allow_dml, allow_ddl = _resolve_write_permission(db_alias, env, config_override)

    if op_type == "DML" and not allow_dml:
        raise ValueError(
            f"DML 操作被拒绝: [{db_alias}] ({env}) 写操作未启用。"
            f"\n请在配置中设置 readonly: false"
        )

    if op_type == "DDL" and not allow_ddl:
        raise ValueError(
            f"DDL 操作被拒绝: [{db_alias}] ({env}) DDL 未启用。"
            f"\n请在配置中设置 allow_ddl: true"
        )

    return op_type


# ── 表名通配符匹配 ──────────────────────────────────────────

def _filter_by_pattern(tables: list, pattern: str) -> list:
    """根据通配符模式过滤表名列表（纯函数，不访问数据库）。
    返回 (matched, is_multi)。
    """
    if pattern.upper() == "ALL":
        return tables
    if "*" in pattern or "?" in pattern:
        return [t for t in tables if fnmatch.fnmatch(t, pattern)]
    return None  # 精确表名，不需要过滤


def _resolve_table_names(pattern: str, db_alias: str, env: str,
                         config_override: str | None = None,
                         _conn=None) -> list:
    """解析表名参数，支持 ALL / 通配符(*, ?) / 精确表名。"""
    is_multi = pattern.upper() == "ALL" or "*" in pattern or "?" in pattern
    if is_multi:
        tables = list_tables(db_alias, env, config_override, _conn=_conn)
        result = _filter_by_pattern(tables, pattern)
        if not result:
            print(f"[WARN] 没有匹配 '{pattern}' 的表", file=sys.stderr)
        return result
    else:
        return [pattern]


# ── 结构化命令处理（--desc / --ddl 共用）─────────────────

def _handle_structure_cmd(args, mode: str):
    """处理 --desc 或 --ddl 命令，抽取公共逻辑。

    mode: "desc" 表格模式 / "ddl" DDL 模式
    """
    env = args.env or DEFAULT_ENV
    label = f"{args.db_alias} ({env})"
    target = args.desc if mode == "desc" else args.ddl

    # —— 单条连接复用: ALL/通配符模式 ——
    pattern_multi = target.upper() == "ALL" or "*" in target or "?" in target
    shared_conn = None

    try:
        if pattern_multi:
            shared_conn, _ = _open_raw_connection(args.db_alias, env, args.config)
            tables = _resolve_table_names(target, args.db_alias, env, args.config,
                                          _conn=shared_conn)
        else:
            tables = [target]

        for i, t in enumerate(tables):
            if i > 0:
                print()

            tbl_comment = get_table_comment(args.db_alias, t, env, args.config,
                                            _conn=shared_conn)
            comment_str = f"  COMMENT: {tbl_comment}" if tbl_comment else ""
            print(f"━━━ {t}{comment_str} ━━━  {label}")

            if mode == "desc":
                print()
                cols, rows = describe_table(args.db_alias, t, env, args.config,
                                            _conn=shared_conn)
                print(f"── 列 ({len(rows)}) ──")
                format_output(cols, rows, args.format, show_row_count=False)

                print()
                print("── 索引 ──")
                icols, irows = describe_indexes(args.db_alias, t, env, args.config,
                                                _conn=shared_conn)
                if irows:
                    format_output(icols, irows, args.format, show_row_count=False)
                else:
                    print("  (无显式索引)")

            elif mode == "ddl":
                print()
                ddl = show_create_table(args.db_alias, t, env, args.config,
                                       _conn=shared_conn)
                print(ddl)

    except Exception as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        if shared_conn:
            shared_conn.close()


# ── 入口 ─────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="数据库查询工具 —— 多环境 SQL 查询/写入 (MySQL/PostgreSQL/SQLite)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python query.py mydb "SELECT * FROM users"                   # 默认 dev
  python query.py --env test mydb "SELECT * FROM users"        # test 环境
  python query.py --env prod mydb "SELECT * FROM users"        # prod 环境
  python query.py --limit 500 --env prod mydb "SELECT ..."     # 指定行数
  python query.py mydb "SELECT ..." --count                    # 只看行数
  python query.py mydb "SELECT ..." --timeout 30               # 30s 超时
  python query.py mydb "DELETE FROM logs WHERE id<100" --limit 500 --dry-run  # 预览
  python query.py --list                                       # 扫描所有环境
  python query.py --list --env prod                            # 只看 prod
  python query.py --env prod mydb --show                       # 列出 prod 全部表
  python query.py mydb --show "user_*"                        # 通配符匹配表名
  python query.py mydb -s user_info                           # 查单表元信息
  python query.py mydb --desc goods_gift                       # 查看表结构（表格）
  python query.py mydb --desc ALL                               # 全部表结构
  python query.py mydb --desc "user_*"                      # 通配符匹配
  python query.py mydb --ddl user_info                      # 查看 DDL
  python query.py mydb --ping                                    # 连接测试
  python query.py --keychain-set mydb --env prod               # 存密码
        """,
    )
    parser.add_argument("db_alias", nargs="?", help="数据库别名")
    parser.add_argument("sql", nargs="?", help="SQL 语句 (SELECT/INSERT/UPDATE/DELETE/REPLACE)")
    parser.add_argument(
        "--env", "-e", metavar="ENV",
        help="目标环境: dev / test / prod (默认: dev, 可通过 DB_QUERY_DEFAULT_ENV 环境变量修改)"
    )
    parser.add_argument(
        "--config", "-c", metavar="FILE",
        help="指定独立配置文件路径 (如 prod.yaml)"
    )
    parser.add_argument(
        "--format", "-f", choices=["table", "json", "csv"], default="table",
        help="输出格式 (默认: table)"
    )
    parser.add_argument(
        "--list", "-l", action="store_true",
        help="列出所有已配置的数据库连接"
    )
    parser.add_argument(
        "--show", "-s", nargs="?", const="ALL", default=False, metavar="TABLE",
        help="列出数据库表 (可指定表名 / 通配符: user_*, 默认 ALL)"
    )
    parser.add_argument(
        "--desc", "-d", metavar="TABLE",
        help="查看表结构 (TABLE=表名 / ALL=全部表 / user_*=通配符)"
    )
    parser.add_argument(
        "--ddl", metavar="TABLE",
        help="查看建表 DDL (TABLE=表名 / ALL=全部表 / user_*=通配符)"
    )
    parser.add_argument(
        "--ping", action="store_true",
        help="测试数据库连接是否可用"
    )
    parser.add_argument(
        "--keychain-set", dest="keychain_set", metavar="ALIAS",
        help="将密码存入 macOS Keychain"
    )
    parser.add_argument(
        "--keychain-get", dest="keychain_get", metavar="ALIAS",
        help="从 macOS Keychain 读取密码"
    )
    parser.add_argument(
        "--limit", metavar="N", type=int,
        help=f"限制行数 (SELECT 默认: {DEFAULT_LIMIT}; DELETE/UPDATE 需手动指定)"
    )
    parser.add_argument(
        "--no-limit", action="store_true",
        help="取消自动 LIMIT 限制（⚠️ 大表可能卡死）"
    )
    parser.add_argument(
        "--count", action="store_true",
        help="只执行 COUNT(*) 预估行数，不取数据（仅对 SELECT 有效）"
    )
    parser.add_argument(
        "--timeout", metavar="N", type=int,
        help="查询超时时间 (秒)，超时自动断开"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="预览 DML/DDL 操作，显示 EXPLAIN + SQL 但不执行"
    )

    args = parser.parse_args()

    # ── 解析环境 ──
    env = args.env or DEFAULT_ENV

    # ── Keychain 操作 ──
    if args.keychain_set:
        _handle_keychain_set(args.keychain_set, env, args)
        return
    if args.keychain_get:
        _handle_keychain_get(args.keychain_get, env)
        return

    # ── --list ──
    if args.list:
        list_env = args.env  # 可选, 过滤特定环境
        list_connections(list_env, args.config)
        return

    # ── 需要 db_alias ──
    if not args.db_alias:
        parser.print_help()
        sys.exit(1)

    # ── --ping ──
    if args.ping:
        try:
            start = time.time()
            conn, db_type = _open_raw_connection(args.db_alias, env, args.config)
            try:
                cursor = conn.cursor()
                cursor.execute("SELECT 1")
                elapsed = time.time() - start
                print(f"✅ [{args.db_alias}] ({env}) 连接成功 ({db_type}) - {elapsed:.3f}s")
            finally:
                conn.close()
        except Exception as e:
            print(f"❌ [{args.db_alias}] ({env}) 连接失败: {e}", file=sys.stderr)
            sys.exit(1)
        return

    # ── --show ──
    if args.show is not False:  # --show 或 --show TABLE_PATTERN
        try:
            pattern = args.show if isinstance(args.show, str) and args.show else None
            cols, all_rows = list_tables_with_info(args.db_alias, env, args.config)

            # 通配符过滤
            if pattern and (pattern.upper() == "ALL" or "*" in pattern or "?" in pattern):
                if pattern.upper() != "ALL":
                    all_rows = [r for r in all_rows if fnmatch.fnmatch(str(r[0]), pattern)]
            elif pattern:
                all_rows = [r for r in all_rows if str(r[0]) == pattern]

            label = f"{args.db_alias} ({env})"
            print(f"━━━ {label} — {len(all_rows)} 张表 ━━━")
            if all_rows:
                format_output(cols, all_rows, args.format, show_row_count=False)
            else:
                print("  (无匹配的表)")
        except Exception as e:
            print(f"[ERROR] {e}", file=sys.stderr)
            sys.exit(1)
        return

    # ── --desc ──
    if args.desc:
        _handle_structure_cmd(args, "desc")
        return

    # ── --ddl ──
    if args.ddl:
        _handle_structure_cmd(args, "ddl")
        return

    # ── 查询 / 写操作 ──
    if not args.sql:
        parser.print_help()
        sys.exit(1)

    try:
        op_type = validate_sql(args.sql, args.db_alias, env, args.config,
                               dry_run=args.dry_run)
        sql = args.sql.strip()
        is_write = op_type in ("DML", "DDL")

        # --count 仅对 SELECT 有效
        if args.count:
            if is_write:
                print("[ERROR] --count 仅适用于 SELECT 语句", file=sys.stderr)
                sys.exit(1)
            cnt = _execute_count(args.db_alias, sql, env, args.config)
            est, summary = _get_explain_info(args.db_alias, sql, env, args.config)
            print(f"环境: {env}")
            print(f"COUNT(*): {cnt:,}")
            if summary:
                print(f"📊 EXPLAIN: {summary}")
            return

        # 写操作安全检查
        if is_write:
            _check_where_clause(sql, op_type)
            conn_info = get_connection(args.db_alias, env, args.config)
            db_type = conn_info["type"].lower()

            # EXPLAIN 预估（UPDATE/DELETE 也有效，INSERT 不需要）
            if not sql.upper().startswith("INSERT"):
                est, summary = _get_explain_info(args.db_alias, sql, env, args.config)
                if summary:
                    print(f"📊 [{env}] EXPLAIN: {summary}")
                if est is not None and est > LARGE_TABLE_THRESHOLD:
                    print(f"   ⚠️  预估影响 {_format_number(est)} 行 (大表)")

            # --dry-run 预览
            if args.dry_run:
                print(f"\n🔍 [DRY-RUN] 以下操作未实际执行:")
                print(f"    SQL: {sql[:300]}")
                return

            # 确认提示
            _confirm_write(env, args.db_alias, conn_info, sql, op_type)

            # --limit 注入 (仅 DELETE/UPDATE)
            effective_limit = None
            upper = sql.upper()
            if args.limit is not None and args.limit > 0:
                if (upper.startswith("DELETE") or upper.startswith("UPDATE")):
                    if not _has_limit(sql):
                        sql = _inject_limit(sql, args.limit, db_type)
                        effective_limit = args.limit

        else:
            # SELECT 分支 —— 原有逻辑
            est, summary = _get_explain_info(args.db_alias, sql, env, args.config)
            effective_limit = None

            has_limit = _has_limit(sql)
            if args.no_limit:
                pass
            elif args.limit is not None:
                if args.limit > 0:
                    if not has_limit:
                        sql = _inject_limit(sql, args.limit, db_type="mysql")
                    effective_limit = args.limit
            elif not has_limit:
                sql = _inject_limit(sql, DEFAULT_LIMIT, db_type="mysql")
                effective_limit = DEFAULT_LIMIT

            if est is not None and est > LARGE_TABLE_THRESHOLD:
                print(f"⚠️  [{env}] EXPLAIN: {summary}")
                if effective_limit and not args.no_limit:
                    print(f"   预估 {_format_number(est)} 行 (大表)，已自动 LIMIT {effective_limit}")
            elif summary:
                print(f"📊 [{env}] EXPLAIN: {summary}")

        # 执行
        columns, rows = execute_query(args.db_alias, sql, env, args.config,
                                      _timeout=args.timeout, _op_type=op_type)

        if is_write:
            print(f"✅ Query OK, {rows} row(s) affected")
        else:
            format_output(columns, rows, args.format)
            if effective_limit and len(rows) == effective_limit:
                print(f"(已截断至 {effective_limit} 行，数据可能不完整 • --no-limit 查看全部)")

    except Exception as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        sys.exit(1)


def _handle_keychain_set(alias: str, env: str, args):
    if sys.platform != "darwin":
        print("[ERROR] Keychain 仅支持 macOS", file=sys.stderr)
        sys.exit(1)
    if alias:
        pass  # 用位置参数提供的别名
    else:
        alias = args.db_alias
    if not alias:
        print("[ERROR] 请提供别名: --keychain-set <别名>", file=sys.stderr)
        sys.exit(1)
    import getpass
    service = _keychain_service(env, alias)
    pwd = getpass.getpass(f"请输入 [{alias}] ({env}) 密码: ")
    subprocess.run(
        [
            "security", "add-generic-password",
            "-a", "db-skills",
            "-s", service,
            "-w", pwd,
            "-U",
        ],
        check=True,
    )
    print(f"密码已存入 Keychain (service={service})")


def _handle_keychain_get(alias: str, env: str):
    if sys.platform != "darwin":
        print("[ERROR] Keychain 仅支持 macOS", file=sys.stderr)
        sys.exit(1)
    try:
        pwd = _resolve_password(env, alias)
        print(pwd)
    except RuntimeError as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
