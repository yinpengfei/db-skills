# db-query — Multi-Environment Database Query CLI for AI Agents

A universal **database CLI tool** / **database skill** that lets **Claude Code**, **OpenCode**, **Hermes**, **Cursor**, **WorkBuddy**, and any AI coding assistant execute read-only SQL queries using pre-configured database aliases across multiple environments. Use it as an **AI agent database tool** (db skill / database skills) — stop pasting host/port/user to your AI; just tell it the alias and query.

[中文](./README.md)

## Supported AI Assistants

Works with any AI agent that can execute shell commands:

| AI Assistant | Usage |
|-------------|-------|
| **Claude Code** | `python3 scripts/query.py my_db "SELECT ..."` |
| **OpenCode** | Execute in terminal, or use `/run` command |
| **Hermes** | Register as custom tool, or shell-exec in conversation |
| **Cursor Agent** | Terminal mode, or declare in `.cursorrules` |
| **WorkBuddy** | Install as skill, auto-detected by AI |
| **GitHub Copilot** | CLI mode execution |
| **Aider** | `/run python3 scripts/query.py ...` |
| **Tongyi Lingma / Comate** | Terminal execution |

It's just a shell command — no platform-specific API or plugin dependency.

## Features

- **Multi-Environment Isolation**: `dev` / `test` / `prod` with separate config files, switch with `--env`
- **Secure Password Management**: Three-tier lookup — macOS Keychain → `.env` file → env vars. Passwords are **never stored in YAML**
- **Large Table Protection**: Auto EXPLAIN before query, warning for >50K rows, auto-injected `LIMIT 100`
- **EXPLAIN Index Insights**: Shows `type=ref | key=idx_mobile | rows=42`. Full table scans marked `type=ALL`
- **Table Structure (3 Modes)**: `--show` for overview (with comments + row counts), `--desc` for columns + indexes (table format), `--ddl` for full `CREATE TABLE` statements
- **Wildcard Batch Operations**: `--show "user_*"`, `--desc "order_*"`, `--ddl "log_*"` — match multiple tables at once
- **Query Logging**: All SQL (including EXPLAIN) auto-logged to `logs/YYYY-MM-DD.log` with full traceability
- **Connection Reuse**: Batch operations (`--desc ALL` / `--ddl ALL`) use only 1 connection for N tables
- **Read-Only**: SELECT / SHOW / DESCRIBE / EXPLAIN only. INSERT/UPDATE/DELETE/DROP are rejected

## Quick Start

### 1. Install Dependencies

```bash
pip install pyyaml pymysql
# For PostgreSQL:
pip install psycopg2-binary
```

### 2. Configure Connections

```bash
# Copy config templates (safe to re-run, won't overwrite existing)
cp -n assets/connections.dev.yaml.example  assets/connections.dev.yaml
cp -n assets/connections.test.yaml.example assets/connections.test.yaml
cp -n assets/connections.prod.yaml.example assets/connections.prod.yaml

# Edit with your host / user / database
vim assets/connections.dev.yaml
```

Example `connections.dev.yaml`:

```yaml
connections:
  my_db:
    type: mysql
    host: 127.0.0.1
    port: 3306
    user: root
    password: ${MY_DB_PASS}   # References .env variable, never hardcoded
    database: mydb
```

### 3. Configure Passwords (choose one)

| Method | Command | Security |
|--------|---------|----------|
| **A. Keychain** | `python3 scripts/query.py --keychain-set --env dev my_db` | ⭐⭐⭐ System-level encryption |
| **B. .env file** | `echo "MY_DB_PASS=xxx" >> assets/.env` | ⭐⭐ Local file |
| **C. Env var** | `export MY_DB_PASS=xxx` | ⭐ CI/CD injection |

### 4. Verify Connection

```bash
python3 scripts/query.py my_db --ping
# → ✅ [my_db] (dev) connected (mysql) - 0.012s
python3 scripts/query.py --env prod my_db --ping
# → ✅ [my_db] (prod) connected (mysql) - 0.008s
```

## Usage Examples

```bash
# ═══ Query ═══
python3 scripts/query.py my_db "SELECT * FROM users WHERE status=1"
# → 📊 [dev] EXPLAIN: type=ref | key=idx_status | rows=156
# → Auto LIMIT 100 applied

python3 scripts/query.py --env prod my_db "SELECT COUNT(*) FROM orders"

# --count: row count only, no data returned
python3 scripts/query.py my_db "SELECT * FROM orders WHERE status=1" --count

# --no-limit for full dataset
python3 scripts/query.py my_db "SELECT * FROM config" --no-limit

# --timeout for slow query protection
python3 scripts/query.py my_db "SELECT * FROM big_table" --timeout 30

# ═══ Browse Tables ═══
python3 scripts/query.py my_db --show                     # All tables (comment + row count)
python3 scripts/query.py my_db -s "user_*"                # Wildcard filter
python3 scripts/query.py my_db -s "user_*" --format json  # JSON output

# ═══ Table Structure ═══
python3 scripts/query.py my_db -d users                   # Structure (columns + indexes)
python3 scripts/query.py my_db --ddl users                # Full CREATE TABLE DDL
python3 scripts/query.py my_db -d "order_*"               # Wildcard batch

# ═══ Global ═══
python3 scripts/query.py --list                           # Scan all environments
python3 scripts/query.py --list --env prod                # Filter by environment

# ═══ Output Formats ═══
python3 scripts/query.py my_db "SELECT * FROM users" --format json
python3 scripts/query.py my_db "SELECT * FROM users" --format csv
python3 scripts/query.py my_db -d users --format json     # Structure output too
```

## Using with AI Assistants

Once installed, just say in conversation (this is how a **database skill** / **db skills** works):

> Query the orders table in dev/my_db for the last 10 unpaid records

The AI agent will construct and execute `python3 scripts/query.py ...` automatically. This **AI coding assistant database** integration works slightly differently across assistants:

**Claude Code / Cursor Agent / Hermes** — direct conversation, AI will shell-exec:

```
Show me all tables in prod user_db, then describe user_info
```

**For more reliable registration as a custom tool** (e.g., Claude Code custom slash commands, OpenCode commands):

```bash
# Set up an alias for convenience
alias dbq='python3 ~/.workbuddy/skills/db-query/scripts/query.py'
dbq my_db -d users
```

## Command Reference

| Command | Description |
|---------|-------------|
| `<alias> "SQL"` | Execute read-only query |
| `--list` | List configured connections across environments |
| `--show [TABLE]` | List tables (with comment + row count), supports wildcards |
| `-d TABLE / ALL / "pat*"` | Table structure (columns + indexes, table format) |
| `--ddl TABLE / ALL / "pat*"` | Full CREATE TABLE statement |
| `--ping` | Test database connectivity |
| `--count` | Run COUNT(*) only, no data |
| `--limit N` | Override row limit (default: 100) |
| `--no-limit` | Disable auto LIMIT |
| `--timeout N` | Query timeout in seconds |
| `--keychain-set` | Save password to macOS Keychain |
| `--format json/csv` | Output format |

Common flags: `--env dev|test|prod` (default: dev), `--config <file>` (custom config)

## Directory Structure

```
db-query/
├── SKILL.md                           # WorkBuddy skill entry
├── README.md                          # Chinese docs (default on GitHub)
├── README_EN.md                       # English docs
├── scripts/
│   ├── query.py                       # Main script
│   └── test.py                        # Unit tests (no DB required)
├── assets/
│   ├── connections.dev.yaml.example   # Dev template ✅ committed
│   ├── connections.test.yaml.example  # Test template ✅ committed
│   ├── connections.prod.yaml.example  # Prod template ✅ committed
│   ├── .env.example                   # Password template ✅ committed
│   ├── connections.dev.yaml           # ❌ Local config, NOT committed
│   ├── connections.test.yaml          # ❌ Local config, NOT committed
│   ├── connections.prod.yaml          # ❌ Local config, NOT committed
│   └── .env                           # ❌ Password file, NOT committed
├── references/
│   └── drivers.md                     # Driver installation guide
└── logs/                              # ❌ Query logs, NOT committed
    └── YYYY-MM-DD.log
```

## Running Tests

Verify all core logic without a database connection:

```bash
python3 scripts/test.py
# → 64/64 passed 🎉
```

## Security

- `assets/connections*.yaml` and `assets/.env` are in `.gitignore` — **never committed**
- Passwords never stored in YAML; resolved at runtime via Keychain / .env / env vars
- Read-only only: SELECT / SHOW / DESCRIBE / EXPLAIN. All DML is rejected
- Query logs stored locally in `logs/`, never uploaded
- **AI agents must NOT directly read `assets/connections*.yaml` or `assets/.env`** — passwords injected at runtime via env vars

## License

MIT

