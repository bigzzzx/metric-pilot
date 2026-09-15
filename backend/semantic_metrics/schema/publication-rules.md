# Metric Wiki publication rules

Publication is an administrator-controlled transaction.

- Reject stale expected_version values.
- Bind the contract to one enabled, published asset in the same business domain.
- Validate the SQL dialect, visible fields, time configuration and aggregate expression.
- Require every related metric to be published in the same domain.
- Require every claim source to belong to the current metric.
- Require each evidence string to occur verbatim in the immutable source.
- Derive source_refs from validated claims; never accept unverified references from the client.
- Save a complete revision and audit record.
- Commit the authoritative SQLite state before refreshing the derived Milvus index.
- If indexing fails, retain the published Wiki and record a failed retryable index job.
