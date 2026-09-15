# Metric Wiki ingestion rules

1. Raw sources are append-only. Existing source rows must never be updated or deleted.
2. Corrections and later decisions create a new source with a new SHA-256 content hash.
3. Generated catalog snapshots include the metric version and exact contract visible at generation time.
4. User-supplied sources must have a type, title and original content. A URI is provenance metadata, not a substitute for the captured content.
5. The draft model may use only supplied sources and related metric identities.
6. Every factual claim must cite one source ID and include a verbatim evidence substring.
7. Drafts are not searchable and cannot affect query execution before administrator publication.
8. Secrets, credentials and unrestricted row data must not be added as Wiki sources.
