# db-query — 多环境数据库查询工具

WorkBuddy 技能，通过预配置的数据库别名按环境快速执行 SELECT 只读查询，无需记忆 host/port/user，在 AI 对话中直接操作数据库。

## 特性

- **多环境隔离**：`dev` / `test` / `prod` 三套独立配置，一个参数切换
- **安全密码管理**：密码通过 macOS Keychain / `.env` / 环境变量三级查找，**不存 YAML**
- **大表保护**：查询前自动 EXPLAIN 预估行数，全表扫描明确标记，自动注入 LIMIT 100
- **EXPLAIN 索引信息**：显示 `type=ref | key=idx_mobile | rows=42`，一眼看出是否走索引
- **表结构查询**：`--desc` 表格模式 / `--ddl` 完整 DDL，支持通配符批量查看
- **查询日志**：所有 SQL 自动记入 `logs/YYYY-MM-DD.log`（含 EXPLAIN）
- **仅只读**：SELECT / SHOW / DESCRIBE / EXPLAIN，拒绝任何 DML

## 快速开始

### 1. 安装依赖

```bash
pip install pyyaml pymysql
# PostgreSQL 用户额外安装:
pip install psycopg2-binary
```

### 2. 配置连接

```bash
cd ~/.workbuddy/skills/db-query

# 复制配置模板（-n 不会覆盖已有文件）
cp -n assets/connections.dev.yaml.example  assets/connections.dev.yaml
cp -n assets/connections.test.yaml.example assets/connections.test.yaml
cp -n assets/connections.prod.yaml.example assets/connections.prod.yaml

# 编辑填入你自己的 host / user / database
vim assets/connections.dev.yaml
```

`connections.dev.yaml` 示例：

```yaml
connections:
  my_db:
    type: mysql
    host: 127.0.0.1
    port: 3306
    user: root
    password: ${MY_DB_PASS}   # 引用 .env 变量，不要直接写明文
    database: mydb
```

### 3. 配置密码（三选一）

**方式 A — macOS Keychain（推荐）**

```bash
python3 scripts/query.py --keychain-set --env dev my_db
# 输入密码后存入 Keychain，之后自动读取
```

**方式 B — `.env` 文件**

```bash
cp -n assets/.env.example assets/.env
chmod 600 assets/.env
echo "MY_DB_PASS=your_password" >> assets/.env
```

**方式 C — 环境变量**

```bash
export MY_DB_PASS=your_password
```

### 4. 验证连接

```bash
python3 scripts/query.py my_db --ping
# → ✅ [dev] my_db 连接正常 (0.012s)
```

## 使用示例

```bash
# 基本查询（默认 dev 环境）
python3 scripts/query.py my_db "SELECT * FROM users"
# → 📊 [dev] EXPLAIN: type=ALL (全表扫描) | key=NULL | rows=5.2K
# → 自动加 LIMIT 100

# 切换环境
python3 scripts/query.py --env prod my_db "SELECT COUNT(*) FROM orders"

# 只看行数（不取数据）
python3 scripts/query.py my_db "SELECT * FROM orders WHERE status=1" --count

# 查看表结构
python3 scripts/query.py my_db --desc users           # 表格模式（含字段注释、索引）
python3 scripts/query.py my_db --ddl users            # 完整 CREATE TABLE DDL
python3 scripts/query.py my_db --desc "order_*"       # 通配符批量查看

# 列出所有已配置连接
python3 scripts/query.py --list

# JSON 输出（方便脚本处理）
python3 scripts/query.py my_db "SELECT * FROM users" --format json
```

## 目录结构

```
db-query/
├── SKILL.md                           # WorkBuddy 技能入口
├── README.md                          # 本文档
├── scripts/
│   ├── query.py                       # 主脚本
│   └── test.py                        # 单元测试（无需数据库）
├── assets/
│   ├── connections.dev.yaml.example   # 开发环境配置模板 ✅ 已提交
│   ├── connections.test.yaml.example  # 测试环境配置模板 ✅ 已提交
│   ├── connections.prod.yaml.example  # 生产环境配置模板 ✅ 已提交
│   ├── .env.example                   # 密码模板 ✅ 已提交
│   ├── connections.dev.yaml           # ❌ 本地配置，不提交 Git
│   ├── connections.test.yaml          # ❌ 本地配置，不提交 Git
│   ├── connections.prod.yaml          # ❌ 本地配置，不提交 Git
│   └── .env                           # ❌ 密码文件，不提交 Git
├── references/
│   └── drivers.md                     # 数据库驱动安装说明
└── logs/                              # ❌ 查询日志，不提交 Git
    └── YYYY-MM-DD.log
```

## 运行测试

无需数据库连接即可验证核心逻辑：

```bash
python3 scripts/test.py
# → 64/64 通过
```

## WorkBuddy 使用

安装到 WorkBuddy 后，在对话中直接说：

> 查询 dev 环境的 orders 表，找出最近 10 条未支付记录

AI 会自动调用此技能，执行 SQL 并展示结果。

## 安全说明

- `assets/connections*.yaml` 和 `assets/.env` 已加入 `.gitignore`，**不会被提交**
- 密码不存储在 YAML 中，通过 Keychain / .env / 环境变量运行时注入
- 只允许只读查询，INSERT / UPDATE / DELETE / DROP 等 DML 会被拒绝

## License

MIT
