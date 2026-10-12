# JacRed API endpoints and compatibility targets

This document records the API surface relevant to the adapter and separates observed public-server facts from the local integration-test target.

## Version and compatibility baseline

### Public server observed on 2026-10-12

A direct request to `https://jac.red/version` returned:

- Version: `3.15.0`
- Git SHA: `f43eeba8`
- Git branch: `HEAD`
- Build time: `2026-09-18 14:08:55 UTC`

The endpoint inventory below is anchored to the corresponding source revision: [JacRed source at `f43eeba8`](https://github.com/jacred-fdb/jacred/tree/f43eeba8). It describes routes in that code snapshot; it does not mean every route was independently tested or is publicly reachable.

### Planned local integration-test target

- Public endpoint currently used by the adapter: `https://jac.red`.
- Target for local adapter integration tests: upstream JacRed release/tag [`3.18.0-oss`](https://github.com/jacred-fdb/jacred/releases/tag/3.18.0-oss), the latest upstream release checked on 2026-10-12.
- Local instance and its synchronization/deployment notes: [evgendman/jacred-sync-node](https://github.com/evgendman/jacred-sync-node).

Before a test run, record the local instance's actual `GET /version` response (version, Git SHA and build date). Do not claim that tests cover `3.18.0-oss` unless that runtime version is confirmed. If the installed instance reports another version, record and test the version actually running.

The newer local test target does not change the adapter's configured public source and does not prove that the public `jac.red` instance has the same routes or behavior.

## Search APIs

### Jackett-compatible JSON — the adapter's upstream API

- `GET /api/v2.0/indexers/{status}/results`
- Adapter default: `GET https://jac.red/api/v2.0/indexers/all/results`.
- `status=all` queries the combined index; a specific tracker/indexer name can scope the search.
- The handler accepts general query parameters including `query`, `title`, `title_original`, `year`, category information and `is_serial`; other query values are available to the search request as well.
- The response is JSON with a top-level `Results` collection.
- `apikey` can be supplied as a query parameter or through a supported request header when configured.

The adapter currently consumes this JSON route directly. In v2.2.10 it sends `Accept: application/json` and a project-specific User-Agent. User-Agent observations and the cautious compatibility plan are documented in [user-agent.md](user-agent.md).

### Jackett indexer discovery

- `GET /api/v2.0/indexers` — lists the aggregate JacRed indexer and the configured trackers in a Jackett-style JSON response.

### Torznab XML

- `GET /torznab/api` — primary Torznab route.
- `GET /api/v2.0/indexers/{indexer}/results/torznab/api` — Jackett-compatible alias.
- `GET /api/v1/indexer/{indexer}/newznab` — Newznab/Prowlarr-compatible alias.

The `t` parameter selects actions such as `caps`, `indexers`, `search`, `tvsearch` and `moviesearch`. These endpoints return Torznab XML; this is a different response format from the JSON route consumed by the adapter.

### Prowlarr API

- `GET /api/v1/indexer` — Prowlarr-style indexer list.
- `GET /api/v1/indexer/{id}` — details for the aggregate indexer (`id=1` in the inspected implementation).
- `GET /api/v1/search` — Prowlarr Search Feed in JSON.

### Native JacRed JSON API

- `GET /api/v1.0/torrents` — JacRed-specific search with filters including title/alternate title, exact match, type, tracker, voice, video type, release year, quality and season.
- `GET /api/v1.0/trackers` — available tracker names.
- `GET /api/v1.0/qualitys` — quality lookup/search.
- `GET /api/v1.0/conf` — API-key/configuration compatibility check.

These are separate interfaces. Their existence should not be interpreted as proof that the adapter uses them; the adapter's source-search URL remains `/api/v2.0/indexers/all/results`.

## Status, statistics and synchronization

### Service information

- `GET /health` — basic process health response.
- `GET /version` — version, Git SHA, branch and build timestamp.
- `GET /lastupdatedb` — latest update time found in the FileDB index.
- `GET /health/background-jobs` — currently running background tracker jobs and progress fields.
- `GET /opensearch.xml` — browser OpenSearch descriptor.

### Statistics

- `GET /stats/torrents` — aggregate per-tracker torrent counts.
- `GET /stats/tracks` — Tracks/audio-metadata statistics.
- `GET /stats/meta` — statistics update timestamps.

Statistics output is controlled by `openstats`; a route being present in source does not guarantee public data access.

### Sync API

- `GET /sync/conf` — reports synchronization protocol capabilities.
- `GET /sync/fdb?key=...` — returns matching FileDB buckets/records when sync export is enabled.
- `GET /sync/fdb/torrents` — paged FileDB export used by JacRed synchronization clients.
- `GET /sync/torrents` — legacy route that points callers to `/sync/fdb/torrents`.

Sync data export depends on `opensync`. `/sync/conf` advertising a protocol version does not, by itself, mean bulk export is enabled.

## Web UI and administrative routes

The web application is served at `/`, `/stats`, `/jobs` and `/settings`.

Administrative/development route families also exist in this source snapshot:

- `/api/v1.0/config` and its subroutes — read, validate, diff, parse, render, format and save configuration.
- `/cron/{slug}/{action}` and `/cron/maintenance/*` — tracker parsing, maintenance and background-job controls.
- `/jsondb/save` — trigger a FileDB save.
- `/dev/*` — diagnostic, maintenance, migration and Tracks export operations.

These are not adapter search endpoints. Configuration, cron and development actions can have side effects or expose secrets; do not test them against the public server. The code documents LAN access or a dedicated `X-Dev-Key`/`devkey` for sensitive operations. API search-key handling is separate.

## What we know about the public server — and what we do not

During diagnostics on 2026-10-12, the public server reported version `3.15.0` at `/version`. The root page and `/torznab/api?t=caps` returned HTTP 200 in particular tested requests, while the adapter's JSON-search endpoint had separately returned HTTP 403.

These observations are endpoint-specific. A successful `/version`, root page or Torznab capabilities request does not establish that `/api/v2.0/indexers/all/results` is accessible. A 403 without an explicit expiry does not identify the cause, prove a whole-domain IP block, or disclose when access will be restored. Do not rotate headers or repeatedly probe the public endpoint to work around an active restriction.

## Local integration-test plan

Use the locally installed JacRed instance referenced above for repeatable integration tests. Before testing, capture its `/version` response and record the exact build under test.

1. Verify the JSON route for `status=all` and a specific configured tracker; confirm JSON parsing and the top-level `Results` collection.
2. Exercise representative movie and TV queries, including original/localized titles, year, season/episode, category and pagination where supported.
3. Verify fields the adapter consumes when present: release title, magnet/infohash, quality, voice/language metadata, `ffprobe` details and source update date. Treat optional metadata as optional, not guaranteed for every release.
4. Test missing/invalid API keys and empty or structured queries against the local instance, and confirm how the handler responds.
5. Compare local behavior with the public `3.15.0` source baseline, noting any route, JSON schema or metadata differences rather than assuming compatibility.
6. Test HTTP 401/403/429, timeouts, `Retry-After` and header construction with a local mock HTTP server. Do not generate failure conditions by repeatedly probing public JacRed.

Track test results in the adapter roadmap. Until the target-version tests have actually run, `3.18.0-oss` is a planned local test target, not a claim of completed compatibility.