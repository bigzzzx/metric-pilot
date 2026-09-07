# PostgreSQL 测试数仓部署记录（2026-09-07）

## 部署结果

- 主机：Ubuntu 24.04.4，`152.136.236.79`
- 独立目录：`/home/ubuntu/data-agent-postgres`
- 容器：`data-agent-postgres`，`postgres:16`（实际 PostgreSQL 16.14）
- 数据库：`data_agent_warehouse`
- 监听：`127.0.0.1:5433`，公网 5433 从开发机不可达
- 限额：1 CPU、512 MiB 内存、20 个连接；验收时约占用 18 MiB
- 数据库验收容量：约 10 MB
- 凭证：服务器 `.env`，权限 600；未写入 Git 或输出到日志

Yuxi 的 `postgres` 容器仍为 healthy，继续监听 `127.0.0.1:5432`，未修改其容器、数据库、
Compose 文件或数据目录。

## 数据内容

创建 ODS、DWD、DWS、ADS 四层共 20 张表，覆盖电商、营销、供应链三个业务域：

- ODS：8 张原始业务表
- DWD：6 张维度/事实明细表
- DWS：3 张主题日汇总表
- ADS：3 张交易、留存和营销 ROI 表

主要合成数据量：300 位客户、80 个商品、1200 笔订单、2400 条订单商品明细、
1143 次支付、120 次退款、320 条库存快照、1500 条营销事件。
数据库保存 Schema/table/关键 column comment 和主外键，样例包含跨月、退款、取消、
NULL、零金额和一笔订单多次支付等查询边界。

## 权限

- `warehouse_admin`：初始化和维护所有者。
- `data_agent_reader`：仅登录、连接、Schema usage 和 SELECT；默认只读、30 秒 statement timeout。

验收确认 reader 可以查询 ADS，但 INSERT 被 PostgreSQL 拒绝。终端用户的业务域和表级 ACL
仍必须由 Data Agent 平台执行，数据库共享 reader 账号不能替代平台权限。

## 验收证据

- 容器健康检查通过。
- 4 个目标 Schema、20 张 base table、20 个表注释、全部字段注释和至少 8 个外键通过断言。
- `orders=1200`、`order_items=2400`、`campaign_events=1500` 通过断言。
- 重启新容器后订单数仍为 1200，持久化通过。
- 监听地址只有 `127.0.0.1:5433`；公网探测不可达。
- 使用 tmpfs 临时容器从零执行同一套脚本，成功创建 20 张表，随后临时容器自动删除。

首次启动时 `00-reader-role.sh` 被错误设置为仅宿主所有者可读，容器切换 PostgreSQL 用户后
无法读取。检查确认当时 `schemas=0`、`reader_roles=0`，没有部分业务表；未删除数据目录，
改为 755 后依次补执行初始化并完成验收。`deploy.sh` 已固化正确权限，后续从零初始化冒烟测试通过。

## 下一阶段

本次只部署真实测试数据源，没有把现有 SQLite 查询工具改成 PostgreSQL。
下一阶段需要实现 PostgreSQL Connector：加密连接配置、SSH/直连模式、元数据采集、
SQL 方言和只读执行、平台 ACL 映射、目录发布/RAG，以及面向该测试数仓的端到端问数评测。
