# Roadmap

## Current validation

### 3. Validate against the real multi-client topology
The implementation is now in place. Validate the single adapter with:
- Prowlarr
- Sonarr 1080p
- Sonarr 4K
- Radarr 1080p
- Radarr 4K

The goal is to verify that repeated tests and identical searches do not unnecessarily multiply requests to `jac.red`.

During validation, confirm:
- an empty Prowlarr `t=search&extended=1` request never reaches `jac.red`;
- repeated identical searches within 60 seconds are served from the adapter cache;
- a changed query, category, season/episode, year, limit, or offset still produces an independent response;
- a failed/rate-limited JacRed request is not cached.

## Implemented in v2.2.0–v2.2.3

### 1. Local response for Prowlarr test requests
- Detect generic Torznab `t=search` requests without a real `q`.
- Do not query `jac.red` for the normal connectivity test.
- Return a small synthetic local test release.
- Include a valid RSS `pubDate` in the synthetic release.
- Parse JacRed RFC3339/RFC3339Nano `createTime` values correctly.
- Never use the current time as a fallback for a malformed source date.
- Preserve successful results from one media type when another upstream media-type query is rate-limited; do not cache partial responses.
- Use standard `application/x-bittorrent` for the torrent enclosure.

### 2. Cache identical recent requests
- Add a configurable in-memory response cache (defaults: 180 seconds, 512 entries).
- Include all query parameters that can affect the adapter response in the cache key.
- Ignore only the API key, allowing identical searches from multiple clients to share a result.
- Serialize concurrent identical requests with a per-key in-flight lock.
- Never cache producer exceptions, including JacRed rate limits and server errors.

## Completed earlier

- Existing `jacred-v2-adapter.service` extended from TV-only to TV + Movies.
- Movie Torznab categories supported: 2000, 2010, 2020, 2030, 2040, 2045, 2050, 2060, 2070, 2080, 2090.
- Torznab `movie-search`, `tv-search`, and generic `search` implemented.
- Quality detection priority: ffprobe → JacRed `info.quality` → source title.
- Existing service remains the single adapter on `127.0.0.1:9128/torznab`.


## Implemented in v2.2.3

- Serialize all upstream requests through one global limiter shared across all adapter threads (default quiet period after each completed request: 1000 ms).
- Honor JacRed `Retry-After` after HTTP 429; use a configurable fallback cooldown when the header is absent.
- Fail fast with HTTP 503 and `Retry-After` if upstream queue wait would exceed the configured limit, rather than waiting into a likely Prowlarr timeout.
- Make cooldown, pacing, and cache settings configurable through environment variables.
- Keep partial results when one requested media-type branch succeeds and the other fails; never cache partial responses.
- Parse RFC3339/RFC3339Nano source timestamps and never replace invalid dates with the current time.
