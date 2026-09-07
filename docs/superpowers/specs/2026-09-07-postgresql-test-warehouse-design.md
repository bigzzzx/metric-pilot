# PostgreSQL 测试数仓部署设计

## 目标

在 `ubuntu@152.136.236.79` 上部署一个供 Data Agent 集成测试使用的独立 PostgreSQL 16
数据源。它用于验证远程连接、元数据采集、数仓分层、业务域、只读查询、权限和血缘，
不承载生产数据。

## 现状与约束

- 服务器为 Ubuntu 24.04.4、4 CPU、7.5 GiB 内存，当前约 2.3 GiB 可用；根盘剩余约 11 GiB。
- Docker 29.1.3 和 Compose 2.40.3 已安装，`postgres:16` 镜像已存在。
- Yuxi 的 `postgres` 容器正在使用 `127.0.0.1:5432`，不得修改、重启或复用其数据库和数据目录。
- 新数据库不得暴露公网，不在 Git、命令输出或应用日志中打印密码。
- 测试数据均为合成数据，不复制 Yuxi 或项目现有用户/业务数据。

## 部署边界

独立目录：

```text
/home/ubuntu/data-agent-postgres/
├── docker-compose.yml
├── .env
├── init/
│   ├── 00-reader-role.sh
│   ├── 01-schema.sql
│   ├── 02-permissions.sql
│   ├── 03-demo-data.sql
│   └── 04-column-comments.sql
└── data/
```

容器名为 `data-agent-postgres`，使用已有 `postgres:16` 镜像，绑定
`127.0.0.1:5433:5432`，配置 `restart: unless-stopped` 和 `pg_isready` 健康检查。
容器最多使用 1 CPU、512 MiB 内存和 20 个连接，避免挤占现有 Milvus、模型及 Yuxi 服务。
数据库名为 `data_agent_warehouse`。

`.env` 权限设为 600；初始化 SQL 不包含明文密码。持久化数据只写入上述 `data/`。

## 账号与权限

- `warehouse_admin`：数据库所有者，仅用于初始化和管理员维护。
- `data_agent_reader`：Data Agent 查询账号，只允许连接、查看授权元数据及执行 `SELECT`。

`data_agent_reader` 不拥有建表、写入、删除、授权和创建扩展权限。默认权限保证未来新建的
授权表仍只读。数据库侧权限是第二道防线；终端用户的业务域和表级权限仍由 Data Agent
平台 ACL 校验，不能因为共享数据源账号而绕过。

## 测试数仓

使用四个 Schema 表示分层，覆盖三个业务域和 20 张表：

- `ods`：用户、订单、订单项、支付、退款、商品、库存快照、营销事件等原始表。
- `dwd`：用户、订单、支付、商品、营销事件等清洗明细表。
- `dws`：用户日汇总、商品日汇总、区域日汇总。
- `ads`：交易日报、留存/复购、营销 ROI。

业务域为电商、营销和供应链。数据包含跨月订单、退款、取消、NULL、零金额、一对多支付、
无订单用户和库存变化等边界情况。姓名、手机号、邮箱等均为明显的合成值，并在目录中标记
敏感级别；前端样例仍必须经过平台脱敏，不能依赖测试数据看起来无害。

表和字段使用 PostgreSQL comment 保存完整技术与语义元数据，包括中文说明、粒度、业务域、
主键和敏感性。主外键表示可由数据源采集的 Join；业务推荐 Join、指标口径和跨表血缘继续由
平台目录/知识库维护，避免把所有业务语义塞进数据库约束。

## 连接方式

服务器只监听回环地址。本地开发通过 SSH 隧道访问：

```text
Windows 127.0.0.1:15433
  -> SSH ubuntu@152.136.236.79
  -> Ubuntu 127.0.0.1:5433
  -> data-agent-postgres:5432
```

后续数据源配置应保存主机、端口、数据库、SSL/SSH 模式和加密凭证，不把密码放入前端状态。

## 验收

部署完成必须验证：

1. Yuxi 的 `postgres` 容器、数据库连接数和 5432 监听未受影响。
2. 新容器健康，重启后数据持久化，且公网地址无法直接访问 5433。
3. 管理员可读取 4 个 Schema、表/字段/comment、主外键和样例统计。
4. `data_agent_reader` 可以查询授权表，但 INSERT、UPDATE、DELETE、CREATE 和 DROP 均失败。
5. 合成数据的关键口径有确定结果，可作为后续 PostgreSQL Connector 的回归夹具。
6. 记录目录、容器状态、表数量和只读权限测试，不记录密码或整批样例数据。

本次部署只创建基础设施和测试数仓，不在同一步修改 Data Agent 的 SQLite 查询工具。
连接器、SQL 方言、平台数据源表单和查询执行适配作为下一阶段独立实现和验收。

## 回滚

只有在用户明确要求回滚时，才停止并删除 `data-agent-postgres` 容器。数据目录单独保留，
除非用户再次明确要求删除 `/home/ubuntu/data-agent-postgres/data`。不得运行影响 Yuxi Compose
项目、Docker 全局资源或其他容器的清理命令。
