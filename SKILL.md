---
name: db-query
description: |-
  数据库查询工具。通过预配置的数据库别名按环境快速执行 SELECT 只读查询。
  支持 MySQL / MariaDB / PostgreSQL，输出格式支持 table / json / csv。
  多环境支持: --env dev|test|prod + prod 独立配置文件隔离。
  安全机制: Keychain 三级密码查找、大表自动 LIMIT 100、EXPLAIN 索引分析、查询日志、仅只读。
  功能: --show 表列表(含行数+注释)、--desc 表结构(含索引)、--ddl 建表语句、--ping 连接测试、通配符匹配、--timeout 超时。
  触发场景: 查询指定数据库、列出数据库表、查看表结构、查看建表语句、连接测试、查看已配置连接。
  关键词: 查数据库、数据库查询、SQL 查询、DB query、查询 XX 库、列出数据库表、数据库连接列表、查看表结构、查看DDL、表注释、db skill、db skills、database skill、database skills、数据库 skill、数据库 skills、数据库技能、MySQL CLI、AI 数据库工具、db-query。
agent_created: true
---

# 数据库查询技能 (db-query)

基于预配置连接信息的多环境数据库只读查询工具。连接信息集中配置，查询时只需指定数据库别名 + 环境。

## 触发条件

- 用户要求执行 SQL 查询并指定了数据库别名
- 用户想查看某个数据库的表列表
- 用户想查看已配置了哪些数据库连接

## 多环境架构

每个环境一个独立配置文件，结构完全对称：

| 环境 | 配置文件 | 密码 Keychain 条目 |
|------|---------|-------------------|
| dev (默认) | `connections.dev.yaml` | `db-query/dev/{alias}` |
| test | `connections.test.yaml` | `db-query/test/{alias}` |
| prod | `connections.prod.yaml` | `db-query/prod/{alias}` |

默认环境为 `dev`，可通过环境变量 `DB_QUERY_DEFAULT_ENV=test` 修改。

### 首次配置

```bash
# 1. 复制配置文件 (⚠️ 只做一次！cp -n 不会覆盖已有文件)
cp -n assets/connections.dev.yaml.example assets/connections.dev.yaml
cp -n assets/connections.test.yaml.example assets/connections.test.yaml
cp -n assets/connections.prod.yaml.example assets/connections.prod.yaml

# 2. 编辑填入各环境的 host / user / database
#    Prod 建议额外 chmod 600:
chmod 600 assets/connections.prod.yaml

# 3. 密码 (三选一，按优先级)
#    a) Keychain (推荐):
python scripts/query.py --keychain-set --env dev recharge_db
python scripts/query.py --keychain-set --env test recharge_db
python scripts/query.py --keychain-set --env prod recharge_db
#    b) .env 文件:
cp -n assets/.env.example assets/.env
# 编辑填入 DB_PWD_DEV_RECHARGE_DB=xxx 等
#    c) 环境变量: export DB_PWD_DEV_RECHARGE_DB=xxx
```

**密码不存入任何 YAML 配置文件。** 脚本按优先级自动查找：Keychain > `.env` 变量 > 环境变量。

支持三种密码配置方式：

| 方式 | YAML 配置 | 密码来源 | 适用场景 |
|------|----------|---------|---------|
| 约定查找 | 不写 `password` 字段 | `DB_PWD_{ENV}_{ALIAS}` | 每库一个独立密码 |
| `${VAR}` 占位 | `password: ${PWD_PROD}` | `.env` 的 `PWD_PROD` 变量 | **多库共享密码（推荐）** |
| 环境变量 | `password: ${MY_PASS}` | `os.environ["MY_PASS"]` | CI/CD 注入 |

Keychain 条目格式：`service=db-query/{env}/{alias}`，例如 `db-query/dev/recharge_db`。

### 🔒 安全红线

- **永远不要用 Read 工具直接读取 `assets/connections*.yaml` 或 `assets/.env`。**
- 所有操作（查询、列表、查看配置）一律通过 `scripts/query.py` 脚本执行。
- 要查看已配置连接 → `python scripts/query.py --list`
- 要查看数据库表 → `python scripts/query.py --env dev <别名> --show`
- 要执行查询 → `python scripts/query.py --env dev <别名> "<SQL>"`
- 如果脚本报错提示文件不存在 → 告知用户需要配置，**不要自己去读文件检查**。

## 使用方式

### 查询数据

```bash
python scripts/query.py <db_alias> "SELECT ..."                    # 默认 dev
python scripts/query.py --env test <db_alias> "SELECT ..."          # 切 test
python scripts/query.py --env prod <db_alias> "SELECT ..."          # 切 prod
python scripts/query.py --config my-prod.yaml <db_alias> "SELECT ..." # 自定义配置文件
```

示例：
```bash
  # 日常 dev 查询（不用写 --env）
  python scripts/query.py recharge_db "SELECT id, name FROM users"
  # → 📊 [dev] EXPLAIN: type=ref | key=idx_mobile | rows=42 → 返回前 100 行

# 切到 prod
python scripts/query.py --env prod recharge_db "SELECT COUNT(*) FROM orders"
# → 自动读取 connections.prod.yaml + prod keychain

# 指定限制
python scripts/query.py --env test recharge_db "SELECT * FROM orders" --limit 500

# 只看行数
python scripts/query.py --env prod recharge_db "SELECT * FROM users" --count

# JSON 输出
python scripts/query.py --env dev recharge_db "SELECT * FROM orders" --format json
```

### 大表保护机制

- 执行前自动 EXPLAIN 预估扫描行数 + 索引使用情况（`type=ref | key=idx_mobile | rows=42`）
- 全表扫描明确标记（`type=ALL (全表扫描)`）
- 无 LIMIT 的 SELECT 自动追加 `LIMIT 100`
- 预估行数 > 50K 时显示醒目警告
- `--count` 只跑 COUNT(*) 不取数据
- `--no-limit` 明确需要全量数据时使用

### 查看表结构 (列 + 索引表格格式)

```bash
python scripts/query.py <db_alias> --desc <TABLE>      # 单表结构
python scripts/query.py <db_alias> --desc ALL           # 全部表结构
python scripts/query.py <db_alias> -d "user_*"          # 通配符匹配
python scripts/query.py --env prod <db_alias> -d t_user # prod 环境
```

输出 SHOW FULL COLUMNS（Field/Type/Null/Key/Default/Extra/Comment）+ SHOW INDEX 两张表格。支持 `--format json/csv`。

### 查看建表 DDL

```bash
python scripts/query.py <db_alias> --ddl <TABLE>       # 单表 DDL
python scripts/query.py <db_alias> --ddl ALL            # 全部表 DDL
python scripts/query.py <db_alias> --ddl "order_*"      # 通配符匹配
```

输出完整的 `CREATE TABLE` 语句（含字段注释、索引、主键、ENGINE 等），与 `SHOW CREATE TABLE` 完全一致。

### 连接测试

```bash
python scripts/query.py <db_alias> --ping               # 快速验证连接
python scripts/query.py --env prod <db_alias> --ping    # 指定环境
```

### 查询超时

```bash
python scripts/query.py <db_alias> "SELECT ..." --timeout 30   # 30s 超时
```

### 列出数据库表 (含 COMMENT + 预估行数)

```bash
python scripts/query.py --env dev <db_alias> --show           # 全部表
python scripts/query.py --env dev <db_alias> -s "user_*"      # 通配符匹配
python scripts/query.py --env dev <db_alias> -s user_info     # 单表元信息
python scripts/query.py --env prod <db_alias> -s --format json # JSON 输出
```

输出 Table / Rows / Comment 三列，一目了然库中有哪些表、各表多少数据。

### 列出所有已配置连接

```bash
python scripts/query.py --list              # 扫描所有 connections.*.yaml
python scripts/query.py --list --env prod   # 只看 prod
```

输出示例：
```
默认环境: dev

环境        别名                   类型        主机                 端口     数据库
dev *       recharge_db           mysql       10.18.122.60        3306     recharge
test        recharge_db           mysql       10.18.122.61        3306     recharge
prod        recharge_db           mysql       10.19.xx.xx         3306     recharge
```

## 前置依赖

执行查询前确认依赖已安装，详见 `references/drivers.md`：

- MySQL/MariaDB: `pip install pymysql`
- PostgreSQL: `pip install psycopg2-binary`
- YAML 配置: `pip install pyyaml`（或使用 JSON 格式）

## 安全限制

- **仅允许只读查询**：SELECT / SHOW / DESCRIBE / EXPLAIN
- DML（INSERT/UPDATE/DELETE）会被拒绝执行
- 密码不存储在任何配置文件中
- **严禁将 `assets/connections*.yaml` 或 `assets/.env` 读入 AI 上下文**
- **严禁删除 assets/ 下的任何 .yaml 或 .env 文件**（用户配置文件，`rm -f` 一律禁止）
- 清理操作仅限于 `/tmp`、`tempfile` 创建的临时目录，绝不触碰 `assets/`

## 测试

无需数据库即可验证所有逻辑：

```bash
cd ~/.workbuddy/skills/db-query
python3 scripts/test.py
```

测试覆盖：YAML 加载、`${VAR}` 占位符解析、SQL 校验、密码解析链、SQL 工具函数、CLI 参数、通配符匹配、日志记录。

## 查询日志

所有查询操作自动记录到 `logs/YYYY-MM-DD.log`，包含时间、环境、SQL、行数、耗时、状态：

```
[2026-06-24 16:13:45] dev:qf_test_db | select * from goods_gift limit 3 | 3 rows | 0.009s | OK
```

`logs/` 目录首次查询时自动创建。

**注意：`cp -n` 不会覆盖已有配置文件**，更新技能后重新运行 `cp -n` 是安全的，不会覆盖你已编辑的配置。
