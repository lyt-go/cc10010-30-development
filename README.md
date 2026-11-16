# 数据仓库ETL任务编排平台

面向数据仓库ETL任务开发、作业编排调度、任务依赖解析、源端数据校验、异常任务重跑、血缘分板与运行全景监控的ETL工程管理平台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/            每个业务模块一个页面
│   ├── src/api/              统一请求封装
│   ├── src/stores/           会话与筛选状态
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/          每个业务模块一组接口
│   ├── app/services/         业务规则与状态流转
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm install
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| ETL作业 | `etljob` | 作业定义 | 作业编号、作业名称、源端数据表 |
| 任务编排 | `dag` | 编排图 | 图编号、图名称、拓扑结构 |
| 依赖解析 | `dependency` | 依赖关系 | 关系编号、上游作业、下游作业 |
| 源端校验 | `sourcecheck` | 校验配置 | 配置编号、源端数据表、行数下限 |
| 运行实例 | `runtime` | 运行记录 | 记录编号、作业名称、开始时间 |
| 异常重跑 | `rerun` | 重跑任务 | 任务编号、原始运行编号、重跑区间开始 |
| 基线管理 | `baseline` | 基线定义 | 基线编号、基线名称、承诺完成时间 |
| 分区检测 | `partitioncheck` | 检测规则 | 规则编号、数据表名、期望分区数 |
| 数据比对 | `comparator` | 比对任务 | 任务编号、源表名、目标表名 |
| 元数据存储 | `metastore` | 元数据表 | 表编号、表名、所属数仓层 |
| 资源配置 | `resource` | 资源池 | 池编号、池名称、最大并发数 |
| SLA监控 | `slavalidate` | SLA规则 | 规则编号、适用作业、完成截止时间 |
| SQL模板 | `sqltpl` | 模板定义 / 模板版本 | 模板编号、模板名称、模板类型、模板版本、快照摘要 |
| 数据备份 | `backuprestore` | 备份策略 | 策略编号、数据表名、备份周期 |
| 日志归档 | `logarchive` | 归档策略 | 策略编号、日志类型、保留天数 |
| 通知渠道 | `notifychan` | 渠道配置 | 渠道编号、渠道类型、接收地址 |
| 发布计划 | `publishplan` | 发布单 | 发布编号、发布内容、变更说明 |

## SQL模板版本发布与ETL作业绑定

SQL模板的作业消费链路是「模板定义 → 不可变模板版本 → 作业绑定」：

1. **创建版本**：`POST /api/sqltpl/{模板ID}/versions`，提交 `模板版本`、`SQL片段`、
   `参数占位符`、`参数默认值`。占位符必须与 SQL 片段中的 `{{ 参数名 }}` 完全一致，
   重复占位符、未声明的默认值都会在创建时被拒绝。
2. **发布版本**：`POST /api/sqltpl/{模板ID}/versions/{版本ID}/publish`。发布是原子的：
   同一模板同一时刻只有一个「已发布」版本，其余已发布版本变成「已替代」；
   重复发布同一版本是幂等的。发布时会把模板状态与作业清单的
   `模板发布状态` 一起回写（`已是最新` / `发现新版本`）。
3. **上传 / 导入 / 导出**：`POST /api/sqltpl/{模板ID}/versions/upload` 上传 SQL 文件生成草稿；
   `GET /api/sqltpl/{模板ID}/versions/{版本ID}/file` 下载版本 SQL；
   `GET /api/sqltpl/export/bundle` 打包全部发布版本，`POST /api/sqltpl/import`
   （multipart 或 JSON）再导入。导入按内容指纹去重，SQL 片段逐字保留。
4. **作业绑定**：`POST /api/etljob/{作业ID}/bind-template`，提交 `模板ID`/`模板编号` +
   `模板版本ID`/`模板版本` + `参数绑定`。只有「已发布」与「已替代」版本可绑定；
   参数按快照默认值补全并渲染 SQL。同一作业、同一版本、同一参数重复提交
   只生成一个绑定结果。可选 `对侧作业编号`，要求对侧作业已绑定同一版本。
5. **回查**：`GET /api/etljob/{作业ID}/binding` 返回绑定版本、快照摘要、参数绑定、
   参数绑定摘要与渲染 SQL；`GET /api/etljob` 列表直接带出模板版本、绑定状态与
   模板发布状态。`GET /api/etljob/bindings` 支持按作业或模板过滤绑定结果。

发布新版本不会改写已有作业的绑定：旧作业继续使用原版本快照，列表上只标记
「发现新版本」，由作业自行重新绑定。

核对脚本：

```bash
cd backend && .venv/bin/python scripts/verify_template_binding.py
```

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

领域：数据工程
子方向：数仓与数据湖
