# PostgreSQL test warehouse

This deployment is an isolated synthetic data source for Data Agent integration tests.
It does not share the Yuxi PostgreSQL container or data directory.

## Server layout

- Target: `/home/ubuntu/data-agent-postgres`
- Container: `data-agent-postgres`
- Database: `data_agent_warehouse`
- Server-only listener: `127.0.0.1:5433`
- Persistent data: `/home/ubuntu/data-agent-postgres/data`

## Deploy and verify

Copy this directory to the target path, then run:

```sh
sh deploy.sh
./verify.sh
```

`prepare-env.sh` creates `.env` once with independent random passwords and mode 600.
Never commit or print that file. PostgreSQL initialization scripts only run against an empty data directory;
future schema changes must use explicit migrations.

For Windows development, forward a local port instead of exposing PostgreSQL publicly:

```powershell
ssh -i "D:\Desktop\key\ssh key\ubuntu.pem" -N `
  -L 127.0.0.1:15433:127.0.0.1:5433 ubuntu@152.136.236.79
```

The application should store the reader credential through its encrypted data-source configuration,
not in frontend state or a tracked file.
