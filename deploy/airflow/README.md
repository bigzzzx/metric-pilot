# 可选 Airflow 接入

`metric_pilot_daily_sales.py` 是 Airflow 3 的 TaskFlow DAG 模板，采用[官方 TaskFlow API](https://airflow.apache.org/docs/apache-airflow/stable/tutorial/taskflow.html)。当前没有在服务器安装或启用 Airflow，此模板不代表已部署定时调度。

1. 在 Metric Pilot“数据目录 → 数据生产”登记任务。
2. 为调度专用用户授予输入资产的 discover/query_data 以及 etl.execute 权限。数据库写入凭据依然只配置在 Metric Pilot 后端。
3. 在 Airflow 创建 `metric_pilot` Connection：host 为后端基础地址，login/password 为上述专用平台账号，extra 为 `{"job_id":"任务 ID"}`。推荐通过 Airflow Secrets Backend 保存账号密码。
4. 将 DAG 文件放入现有 Airflow DAG 目录；执行环境需 `httpx`。首次部署保持暂停，由管理员核对时区、网络、任务 ID 和账号权限后启用。
5. DAG 每日申请对 `data_interval_start` 到 `data_interval_end` 的区间重算，等待至多 15 分钟。平台管理员在“数据生产”页面处理待审批任务，仍以申请人的权限和原始参数执行，审计分别记录 requested_by 和 decided_by。
6. 拒绝、过期或执行失败都会使 DAG 明确失败；重试须重新发起并批准。不会自动扩大授权、修改指标口径或绕过逐次审批。

登录 Cookie 只在每个任务的客户端内存中使用，客户端结束时登出；不会进入 XCom。XCom 只传递审批 ID 和成功运行 ID。

已验证的是平台 HTTP 申请/审批/执行/查询接口与角色边界；Airflow 的导入和实际调度需要在选定的 Airflow 部署中再验收。
