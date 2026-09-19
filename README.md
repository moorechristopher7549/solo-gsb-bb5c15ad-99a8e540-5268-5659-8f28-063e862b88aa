# 匿名评审分配系统（Review Assignment · MCMF）

基于 **Python + FastAPI + SQLite + 原生 HTML/CSS/JS** 的单容器 Web 应用。
用**最小费用最大流（Min-Cost Max-Flow）**为稿件分配评审人：

- ✅ **冲突边绝不使用**——作者冲突的“稿件–评审人”边根本不会加入流网络；
- ⚖️ 费用同时惩罚 **主题不匹配**（β × 未覆盖主题数）与 **评审负载偏差**（凸增边际成本 α·k，天然均衡）；
- 🔒 允许**锁定少量既有分配后重新求解**，锁定边预占稿件名额与评审容量并参与负载计费；
- ⛔ 容量不足时给出**未满足稿件清单**和**可复核的原因明细**（冲突排除人数、各候选评审剩余容量与主题覆盖情况、全局容量缺口）；
- 🗃️ SQLite 单文件持久化，数据卷挂载；无前端构建步骤、无第三方 JS 依赖。

---

## 一条命令启动

需要安装 Docker（含 Compose 插件）。在项目根目录执行：

```bash
docker compose up --build -d
```

这一条命令会完成：

1. 构建镜像并安装全部 Python 依赖（`requirements.txt`）；
2. 启动容器时**自动初始化数据库**：建表、写入默认求解参数、载入演示数据（首次启动）；
3. 在后台启动 FastAPI / uvicorn 服务。

启动后访问：

> **http://localhost:8000**

- 前端页面：<http://localhost:8000/>
- 接口总览（Swagger）：<http://localhost:8000/docs>
- 健康/状态接口：<http://localhost:8000/api/state>

停止 / 查看日志：

```bash
docker compose logs -f
docker compose down          # 停止（数据保留在卷 review-data 中）
```

不想要演示数据（空库启动）时，将 `docker-compose.yml` 中的 `SEED_DEMO` 改为 `"0"` 后重建；
或删除卷重新开始：`docker compose down -v`。

### 不使用 Docker（本地开发）

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

数据库默认创建在 `./data/review.db`（可用环境变量 `DB_PATH` 覆盖），首次启动自动建库并写入演示数据。

冒烟测试（仅标准库）：

```bash
python -m tests.test_smoke
```

---

## 使用说明

页面共 4 个标签页：

| 标签页 | 功能 |
| --- | --- |
| **分配结果** | 一键“重新求解分配”；查看每篇稿件的评审人、费用拆解、评审人负载条；🔒 锁定/解锁既有分配；展开未满足稿件的原因明细；调整求解权重与默认值 |
| **稿件** | 录入稿件主题（逗号/顿号分隔）、设置**每稿所需评审数**；编辑、删除 |
| **评审人** | 录入评审人擅长主题与**个人容量**；编辑、删除 |
| **作者冲突** | 用勾选矩阵登记“稿件–评审人”冲突；已锁定的分配需先解锁才能登记冲突 |

顶部按钮：

- **▶ 重新求解分配**：按当前数据与权重运行 MCMF，生成完整分配；
- **载入演示数据**：恢复内置示例（含一个刻意制造的容量不足样例）；
- **清空全部**：删除所有稿件、评审人、冲突与分配。

> 锁定任意分配后系统会**立即自动重新求解**；无需再点“重新求解”。

---

## 优化模型

### 网络结构

```
        cap = 缺口          cap = 1             凸增单位槽位
S ───────────────► M_i ───────────────► R_j ───────────────► T
  (稿件需求-锁定数)        (无冲突且未锁定)      cost = α·k, k=1..容量
```

- **S → 稿件**：容量 = 该稿件尚缺的评审数（需求 − 有效锁定数）；
- **稿件 → 评审人**：容量 1（同一评审对同一稿件至多评审一次）；
  **冲突边与已锁定边不会被创建**，因此从结构上保证绝不使用；
  费用 = `β ×（稿件主题 − 评审擅长主题）的数量`；
- **评审人 → T**：容量拆为单位“负载槽位”，第 k 个槽位费用 `α·k`（k 从该评审锁定数之后连续计）。
  边际成本递增使流算法自动偏好更空闲的评审人——这就是“负载偏差”惩罚。

### 锁定的既有分配

锁定边不进入网络，而是：

1. 预占该稿件一个名额（S→M 容量减 1）；
2. 预占该评审一个容量槽位（自由槽位减少，锁定数超过容量时自由容量为 0，并给出告警）；
3. 按相同规则计入总费用（主题费 + 其所处槽位的负载费），保证锁定与新求解目标口径一致。

非法锁定（指向已删除对象、与冲突边重合、超过稿件需求）会被跳过并在“求解警告”中逐条列出，**不会静默处理**。

### 容量不足时的输出

最大流流量小于总需求时，对每篇未满足稿件返回：

- `required / assigned / deficit`；
- 自然语言原因（冲突排除人数、候选剩余总容量、是否全局容量不足等）；
- `details.candidates`：每位**非冲突**评审人的剩余容量与未覆盖主题数，可人工逐项复核。

### 可调参数（页面“求解参数与默认值”）

| 参数 | 默认 | 含义 |
| --- | --- | --- |
| `mismatch_weight` (β) | 10 | 每个未覆盖稿件主题的费用 |
| `load_weight` (α) | 2 | 第 k 个负载槽位费用系数（越大负载越均衡；设 0 关闭负载惩罚） |
| `default_required_reviews` | 3 | 新建稿件的默认所需评审数 |
| `default_capacity` | 3 | 新建评审人的默认个人容量 |

MCMF 实现见 `app/mcmf.py`（SPFA + Successive Shortest Path，可正确处理残量网络中的负权反向边），
业务建模见 `app/solver.py`。

---

## 项目结构

```
.
├── Dockerfile              # python:3.12-slim，装依赖、暴露 8000
├── docker-compose.yml      # 一条命令启动；SQLite 持久化到命名卷
├── requirements.txt
├── README.md
├── app/
│   ├── main.py             # FastAPI 路由 + 静态文件托管
│   ├── schemas.py          # Pydantic 请求模型/校验
│   ├── db.py               # SQLite 连接、建表、演示数据、主题规范化
│   ├── mcmf.py             # 最小费用最大流（无第三方依赖）
│   ├── solver.py           # 建网、求解、锁定处理、未满足原因分析
│   └── static/
│       ├── index.html      # 原生前端
│       ├── styles.css
│       └── app.js
└── tests/
    └── test_smoke.py       # 标准库冒烟测试
```

## REST API 摘要

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/state` | 一次性拉取全部数据（稿件/评审人/冲突/分配/上次解） |
| POST / PATCH / DELETE | `/api/manuscripts[/{id}]` | 稿件增改删 |
| POST / PATCH / DELETE | `/api/reviewers[/{id}]` | 评审人增改删 |
| POST / DELETE | `/api/conflicts`、`/api/conflicts/{m_id}/{r_id}` | 冲突登记/移除 |
| PUT | `/api/settings` | 权重与默认值 |
| POST | `/api/solve` | 运行 MCMF 求解 |
| POST | `/api/assignments/lock` | 锁定/解锁并立即重新求解 |
| POST | `/api/demo`、`/api/reset` | 载入演示数据 / 清空 |

所有写接口均返回最新的完整状态，前端无需二次拉取。

## 数据持久化

- Docker：SQLite 文件位于容器内 `/data/review.db`，映射到命名卷 `review-data`；
- 本地：默认 `./data/review.db`（已加入 `.gitignore`）。
