# Roadmap

## Next: protect JacRed from redundant requests

### 1. Local response for Prowlarr test requests
- Detect Torznab `t=search` requests without a real `q`.
- Do not query `jac.red` for these requests.
- Return a small local synthetic/test Torznab result so Prowlarr, Sonarr, and Radarr can validate the indexer without consuming JacRed requests.
- Keep real searches with a non-empty `q` unchanged.

### 2. Cache identical recent JacRed requests
- Add a short-lived in-memory cache for JacRed query results.
- The cache key must include every search parameter that can change the result (media type, query, category, year, season, episode, pagination, and relevant flags).
- Identical requests arriving within the cache lifetime must reuse the same JacRed response instead of creating another upstream request.
- Different searches must remain independent.
- The cache is an optimization and protection layer; it must not change the Torznab result format or search semantics.
- Cache failures/rate-limit responses must not be treated as successful search results.

### 3. Validate against the real multi-client topology
After implementation, test the single adapter with:
- Prowlarr
- Sonarr 1080p
- Sonarr 4K
- Radarr 1080p
- Radarr 4K

The goal is to verify that repeated tests and identical searches do not unnecessarily multiply requests to `jac.red`.

## Completed

- Existing `jacred-v2-adapter.service` extended from TV-only to TV + Movies.
- Movie Torznab categories supported: 2000, 2010, 2020, 2030, 2040, 2045, 2050, 2060, 2070, 2080, 2090.
- Torznab `movie-search`, `tv-search`, and generic `search` implemented.
- Quality detection priority: ffprobe → JacRed `info.quality` → source title.
- Existing service remains the single adapter on `127.0.0.1:9128/torznab`.
