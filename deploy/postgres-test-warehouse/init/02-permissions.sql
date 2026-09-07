\set ON_ERROR_STOP on

REVOKE CREATE ON SCHEMA public FROM PUBLIC;
REVOKE TEMPORARY ON DATABASE data_agent_warehouse FROM PUBLIC;

GRANT CONNECT ON DATABASE data_agent_warehouse TO data_agent_reader;
GRANT USAGE ON SCHEMA ods, dwd, dws, ads TO data_agent_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA ods, dwd, dws, ads TO data_agent_reader;

ALTER DEFAULT PRIVILEGES FOR ROLE warehouse_admin IN SCHEMA ods GRANT SELECT ON TABLES TO data_agent_reader;
ALTER DEFAULT PRIVILEGES FOR ROLE warehouse_admin IN SCHEMA dwd GRANT SELECT ON TABLES TO data_agent_reader;
ALTER DEFAULT PRIVILEGES FOR ROLE warehouse_admin IN SCHEMA dws GRANT SELECT ON TABLES TO data_agent_reader;
ALTER DEFAULT PRIVILEGES FOR ROLE warehouse_admin IN SCHEMA ads GRANT SELECT ON TABLES TO data_agent_reader;

ALTER ROLE data_agent_reader SET search_path = ads, dws, dwd, ods, public;
