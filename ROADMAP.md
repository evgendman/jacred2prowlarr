# Roadmap / Implementation status

[English documentation](README.md) | [Документация на русском](README.ru.md)

Current adapter version: **2.2.10**

## Project goal

Provide one local Torznab endpoint for Prowlarr that converts JacRed v2 JSON results into predictable movie and TV release records for Sonarr and Radarr. The adapter addresses Russian-tracker naming, dubbing/voice metadata, category reporting, empty-query connectivity tests, duplicate upstream traffic and request pacing.

This is a metadata/search adapter: the final automatic release decision remains the responsibility of Sonarr/Radarr Quality Profiles and Custom Formats.

## Upstream access incident (2026-10-12)

On 2026-10-12, diagnostic requests to the JacRed v2 JSON endpoint and the root `https://jac.red/` returned `HTTP 403 Forbidden` with an `nginx/1.24.0 (Ubuntu)` response; neither response included `Retry-After` or a stated expiry time. This did not establish the reason or duration of the restriction.

Follow-up tests showed that a request to `https://jac.red/torznab/api?t=caps` using a Chrome 155 User-Agent returned HTTP 200. Later on 2026-10-12, one controlled request from the mini-PC to `https://jac.red/` with the Chrome 111 User-Agent used by the upstream JacRed HTTP client returned `HTTP 200 OK`, from `144.31.3.10`, with `Server: nginx/1.24.0 (Ubuntu)`. This confirms that the root page was reachable under that tested request and makes a blanket IP-level block of the entire domain less likely.

Separate tests on `https://sync.jacred.stream/sync/conf` also found HTTP 403 without an explicit browser-style User-Agent and HTTP 200 with Chrome User-Agent values. Taken together, these observations support a hypothesis that request headers or endpoint-specific security handling affect the outcome. They do not prove that User-Agent is the sole cause, and the successful root/Torznab requests do not establish recovery of the adapter's v2 JSON search endpoint.

Do not treat the entire domain as proven blocked or as fully recovered: endpoint-level reachability is mixed. Avoid repeated live probes and do not rotate headers to bypass an active restriction. Test protection logic against a local mock upstream. The successful root check is recorded in [docs/user-agent.md](docs/user-agent.md); any further recovery attempt for the actual v2 search endpoint should follow the controlled recovery policy.

## Confirmed validation

The following scenarios have been manually tested during development:

- Prowlarr generic connectivity test and the Sonarr/Radarr-specific empty-query tests succeed with local synthetic responses. These test paths do not query JacRed.
- Category-oriented local responses were checked: `cat=2000` returns a movie test item; `cat=5000` returns a TV item; `cat=5070` returns a TV item with Anime category metadata; an unfiltered generic Prowlarr test returns both media types.
- Repeating an identical source search reuses cached results. With TV pagination for `Loki`, the first page fetched source results and the second page reused the raw source-result cache rather than issuing a second identical JacRed query.
- A live Sonarr search for `X Files` detected `Секретные материалы` as a Russian alias based on distinct paired release titles, queried the alias and merged matching X-Files results. Subsequent requests hit the source-result cache.
- v2.2.10 was tested with a real X-Files collection title in Sonarr. The displayed title correctly begins with `X-Files S01-S11 (1993) BDRip 1080p`, while retaining the original JacRed title after `***`.
- Season/episode parser cases were checked:
  - `Секретные материалы (1-11 сезоны: 1-217 серии из 217) / The X-Files / 1993-2018` → `S01-S11`
  - `Секретные материалы 6 сезон (1-22 из 22) / The X-Files (1998-1999)` → `S06E01-22`
  - `The X-Files S11E01-10 WEB-DL 1080p` → `S11E01-10`

These checks confirm the listed scenarios; they do not substitute for validation of every combination of client, quality tier, indexer category and source error condition.

## Remaining end-to-end validation

1. Complete the full intended topology test with the shared adapter and all configured clients:
   - Prowlarr;
   - Sonarr 1080p and 4K;
   - Radarr 1080p and 4K.
2. For the same title searched by multiple clients/quality tiers, confirm the raw source-result cache prevents redundant identical JacRed queries and that distinct local filters still produce correct, independent pages.
3. Verify actual end-to-end automatic selection with the intended Custom Formats, including preferred voice/dubbing scores as well as resolution, source format and HDR. The adapter exposes these signals; scoring policy belongs to Sonarr/Radarr.
4. Implement and test upstream failure handling against a local mock HTTP source: HTTP 429 and both `Retry-After` formats, HTTP 403, HTTP 401, a busy queue, timeouts, and partial movie/TV branch failures. Do not use public JacRed to generate failure cases. Any real-source recovery check must be a single controlled attempt after the protection period.

## Planned: upstream protection and controlled recovery

**Status:** approved design; not implemented in adapter v2.2.10. The current code has a per-process request limiter, a one-second default interval, a fixed fallback cooldown for HTTP 429, and no dedicated circuit-breaker behavior for HTTP 403. The requirements below describe the target implementation, not current capabilities.

### 1. One upstream gateway

- The adapter must be the only component allowed to make requests to public JacRed. Future tools, including `torr2strm`, must use the local adapter rather than calling JacRed directly.
- Only one active adapter instance may issue upstream requests. Legacy and V2 adapters must not run side-by-side with independent limiters.
- If multi-process or multi-host operation is ever supported, it must first gain a cross-process/shared lock and shared state. A Python thread lock and process-local counters are not sufficient.
- Local Torznab connectivity tests must continue to use synthetic responses and must not contact JacRed.

### 2. Strictly sequential upstream requests

- Allow at most one in-flight request to JacRed at a time, regardless of the number of local clients, HTTP handler threads, or search branches.
- Set `JACRED_MIN_INTERVAL_MS` to a default of **5000 ms** and enforce 5000 ms as the minimum permitted setting. The configured quiet interval is measured from completion of the previous upstream attempt to the start of the next one.
- Keep identical-request coalescing and both existing caches; use them to avoid unnecessary upstream traffic.
- If the next slot cannot be granted within `JACRED_MAX_QUEUE_WAIT_SECONDS`, return local HTTP 503 with `Retry-After` and do not make an upstream request.
- A combined movie + TV search must not trigger concurrent upstream branches. A fatal source response must cancel the remaining branches and title variants for that operation.

### 3. Persistent circuit breaker: CLOSED / OPEN / HALF_OPEN

- **CLOSED:** normal operation, still subject to the sequential scheduler and minimum interval.
- **OPEN:** upstream requests are prohibited until the recorded cooldown expires. Local clients receive HTTP 503 with a `Retry-After` matching the remaining block time. Cache hits may still be served if their data is valid; a cache miss must not contact JacRed.
- **HALF_OPEN:** after an eligible cooldown, exactly one controlled probe may run. All other upstream requests remain blocked while the probe is in progress.
- Persist state, block-until timestamp, consecutive failure counters, failure reason, and recovery stage in a small SQLite state file at a durable, service-writable path. Updates must be atomic, and a service restart must not reset an active block or the failure escalation.
- A successful probe does not immediately restore normal speed. Clear failure escalation only after a configurable stable run of successful requests, not after one success.

### 4. HTTP 429: escalating backoff and Retry-After

For `429 Too Many Requests`, stop all upstream traffic and escalate the shared cooldown according to this default sequence:

| Consecutive 429 stage | Default delay |
|---|---:|
| 1 | 60 seconds |
| 2 | 5 minutes |
| 3 | 15 minutes |
| 4 | 1 hour |
| 5 and later | 6 hours |

- Parse `Retry-After` both as seconds and as an HTTP date.
- Never retry earlier than either the internally calculated backoff or the source's requested wait; use the longer remaining delay.
- Keep the delay sequence configurable, with the values above as defaults. Preserve compatibility with the existing `JACRED_429_BACKOFF_SECONDS` setting or document its replacement clearly.
- Do not reset the failure level after a single success. Use the controlled recovery process described below.
- Do not cache a 429 response as a successful search result.

### 5. HTTP 403 and 401 handling

**First HTTP 403:**

- Immediately open the circuit for **24 hours** and stop all other upstream requests, including remaining title variants and branches of the current search.
- Do not cache the response as a successful result.
- Log a CRITICAL event with timestamp, status, safe response headers, a bounded response-body excerpt, and the block expiry. Return local HTTP 503 with `Retry-After` to clients while blocked.

**Probe after the first 24-hour block:**

- Permit exactly one controlled probe in HALF_OPEN.
- If it succeeds, enter gradual recovery.
- If it returns 403 again, block upstream traffic for **7 days** and require a manual decision before any further probe. Do not resume probing automatically when those seven days expire.

The 24-hour and 7-day periods are local safety policy, **not a claim about JacRed's actual ban duration**. A 403 response without explicit timing headers does not disclose when access will be restored.

For HTTP 401, stop upstream requests and require manual inspection of authentication/configuration. Do not repeatedly retry unchanged credentials.

### 6. Gradual speed recovery

After an eligible HALF_OPEN probe succeeds, pace subsequent upstream requests using these intervals:

**60 → 30 → 15 → 10 → 5 seconds.**

Advance one step only after **three consecutive successful upstream requests** at the current step. A new 429 returns the circuit to OPEN and increases the failure stage; a new 403 follows the stricter 403 policy. The normal 5-second floor remains in force. Make the recovery steps and success count configurable.

### 7. User-Agent compatibility

- Adapter v2.2.10 currently sends the project-specific value `JacRed-V2-Torznab-Adapter/{VERSION}`.
- In the next adapter version, replace this default with a stable value aligned with the User-Agent used by the upstream JacRed HTTP client. In the upstream source inspected on 2026-10-12, `Infrastructure/Networking/HttpClient.cs` used `Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/111.0.0.0 Safari/537.36`. Recheck the then-current upstream source before implementation; this is a compatibility reference, not a permanent API contract.
- If configurable, expose it as `JACRED_USER_AGENT`, document the fixed default, and keep the value stable across all upstream requests.
- Add a local-mock test confirming that the selected User-Agent is sent consistently. Do not rotate values randomly or change the header in reaction to each error.
- The observed responses do not prove that User-Agent alone caused or resolves all 403 responses. Do not use this change to probe repeatedly or bypass an active 403 restriction. Keep the existing cooldown and single controlled recovery-check policy; see [docs/user-agent.md](docs/user-agent.md).

### 8. Diagnostics and safe logging

For each upstream attempt and circuit-state change, log UTC timestamp, operation/request correlation ID, queue wait, request duration, HTTP status or exception class, state transition, failure stage, calculated cooldown, and next eligible request time. Record `Retry-After`, `Content-Type`, `Content-Length`, `Server`, `Date`, and available request/correlation IDs when present.

For error bodies, log only a bounded excerpt adequate for diagnosis. Never log API keys or other secrets. Full search parameters remain opt-in and must redact credential-like values. Logs must make it possible to establish the order and timing of 429/403 responses and determine whether retries continued after the first fatal response.

### 9. Cache behavior while protected

- Retain the existing rendered Torznab-response cache and raw JacRed-result cache.
- Serve valid cached results during OPEN when the requested result can be produced entirely from cache.
- On cache miss during OPEN, return local 503 with `Retry-After`; never make an upstream request.
- Never cache upstream errors, timeouts, or partial combined-search results as complete successful results.
- Cache and request coalescing do not replace the global sequential scheduler or persisted circuit-breaker state.

### 10. Required automated tests before release

Use a local mock HTTP upstream for these cases; do not deliberately provoke rate limiting or 403 errors on public JacRed.

1. Concurrent local requests never create simultaneous upstream requests.
2. The configured upstream interval can never be lower than 5000 ms.
3. Identical searches are coalesced and cached; different local filters reuse raw results where applicable.
4. Both `Retry-After` formats are parsed, longer source delays are respected, and the 429 sequence escalates as specified.
5. The first 403 cancels all remaining search branches and variants, opens a 24-hour block, and prevents other clients from contacting the source.
6. The single HALF_OPEN probe is enforced under concurrent client load; a second 403 establishes the seven-day block and manual-review requirement.
7. HTTP 401 requires manual intervention and cannot trigger uncontrolled retries.
8. Valid cache hits work during OPEN, while cache misses return local 503 without upstream traffic.
9. Restarting the service preserves OPEN state, cooldown expiry, counters, and recovery stage.
10. Successful probes use the gradual recovery sequence; subsequent 429/403 responses reset protection as specified.
11. Combined movie/TV requests do not run upstream branches concurrently or cache a partial result as success.
12. Local Prowlarr/Sonarr/Radarr connectivity tests still never contact JacRed.
13. Logs preserve enough information to reconstruct failure timing without exposing secrets.

### 11. Configuration/documentation update

When implementing this design, update the English and Russian READMEs and the deployment instructions alongside the code. Document all new parameters and defaults, persistent-state location and permissions, state transitions, response behavior during OPEN, log interpretation, and the manual recovery procedure. Do not describe these target behaviors as implemented until the code and mock-upstream tests confirm them.

## Implemented behavior

### Media types, categories and Torznab

- One existing systemd service was extended from TV-only to movies and TV; endpoint: `http://127.0.0.1:9128/torznab`.
- Supports generic `search`, `tv-search` and `movie-search`.
- TV parent category `5000` and Anime subcategory `5070`; movie parent `2000` and supported movie source categories `2010, 2020, 2030, 2040, 2045, 2050, 2060, 2070, 2080, 2090`.
- Movie and TV records use separate title-formatting branches.
- Movie subcategory filtering does not discard a release merely because JacRed only supplied its parent category; unsupported source details are not invented.
- Quality detection priority: ffprobe dimensions → JacRed `info.quality` → release title. Exposes HDR for 2160p when supported markers are present, and video resolution/codec metadata when available.
- Voice metadata is taken in priority order from Russian audio-stream tags in ffprobe, JacRed `info.voices`, or supported MVO patterns in the source title. Available language information is also exposed in Torznab metadata.
- Formatted title retains the original source title after `***` when it differs from the normalized title, and the original title is also the Torznab description.
- Standard torrent enclosure MIME type, magnet URL, infohash and source date parsing.

### Russian alternate-title search for TV

- For a non-Cyrillic TV query, examine distinct JacRed release titles for recurring localized/original title pairs.
- Require more than one independent release and a clear majority before accepting a Russian alias.
- Query JacRed with that alias, retain only matching releases for the canonical series title, merge with the original results and deduplicate by infohash.
- Canonical series title remains in normalized output; alias search expands discovery rather than changing the title expected by Sonarr.

### Multi-season and episode parsing

- Parse explicit season/episode and episode-range patterns before generic season markers.
- Recognize collection ranges such as `1-11 сезоны` and `Seasons 1-11` as `S01-S11`, rather than misreading the last number as a single season.
- Preserve multi-episode output such as `S06E01-22` and explicit notation such as `S11E01-10`.

## Version implementation history

### v2.2.0–v2.2.3: local tests, caching and upstream safety

- Detect recognized generic Torznab connectivity requests without a real `q` and return a local synthetic test item rather than contacting JacRed.
- Match the synthetic result to the client/request type: movie for movie search, TV for TV search, or both for an unfiltered generic Prowlarr test.
- Keep structured empty-query searches (IDs, year, season or episode) empty; never invent a fallback title and never send an empty search to JacRed.
- Include a valid RSS publication date in the synthetic feed.
- Parse RFC3339/RFC3339Nano source dates; never use the current time as a fallback for malformed or absent dates.
- Cache complete rendered Torznab responses in memory (defaults: 180 seconds, 512 entries); key all request parameters that affect the response while ignoring only the API key.
- Coalesce concurrent identical rendered requests and never cache upstream failures, rate-limit responses or partial results.
- Serialize outbound JacRed requests through a global limiter (default minimum quiet interval: 1000 ms).
- Honor `Retry-After` after HTTP 429, use a configurable fallback cooldown, and return HTTP 503 with `Retry-After` if the upstream queue wait would exceed its limit.
- Preserve successful movie/TV branch results if the other branch of a generic request fails, but do not cache a partial response.
- Use the standard `application/x-bittorrent` type for torrent enclosures.

### v2.2.4: no invented search query

- Treat a category-only generic Prowlarr test as a local connectivity test.
- Remove the invented `the gentlemen` fallback.
- Return an empty feed for other requests without a query instead of fabricating an upstream search.

### v2.2.5: category-aware synthetic tests

- Reflect the requested Torznab media type and supported category in local test items.
- Include the Anime category in the synthetic TV item when `5070` is explicitly requested.

### v2.2.6: opt-in diagnostics

- Add `JACRED_LOG_REQUEST_PARAMS=1` for troubleshooting request parameters and a short canonical-cache-key signature.
- Keep the feature off by default and redact credential-like parameters.

### v2.2.7: raw source-result cache

- Add a separate cache for raw JacRed results, keyed by source query, year and media type.
- Reuse one raw result set across Torznab offsets and local category/season/episode filters.
- Coalesce concurrent requests for the same source-cache key and distinguish `SOURCE CACHE HIT` from a rendered-response `CACHE HIT`.

### v2.2.8–v2.2.9: Russian TV alias discovery

- Detect a recurring Russian alias from multiple distinct JacRed release names that pair the queried international title with a localized title.
- Query JacRed using the discovered alias, filter to the canonical series and merge/deduplicate results.
- Correct the title normalization patterns and keep the source release title intact for the `***` suffix and season/episode parsing.

### v2.2.10: multi-season collection fix

- Parse explicit multi-season collection ranges such as `1-11 сезоны` and `Seasons 1-11` as `S01-S11`.
- Preserve the higher priority of explicit season/episode and multi-episode patterns.
- Live Sonarr validation confirmed the X-Files 1–11 season pack title formats as `X-Files S01-S11 (1993) BDRip 1080p` and retains the full source title after `***`.

## Operational defaults

The table below documents the **current v2.2.10 implementation**, not the planned protection redesign above. In particular, the current upstream interval remains 1000 ms and the full persistent circuit breaker is not implemented yet.

| Environment variable | Default | Purpose |
|---|---:|---|
| `JACRED_MIN_INTERVAL_MS` | `1000` | Minimum quiet interval between upstream requests |
| `JACRED_429_BACKOFF_SECONDS` | `60` | Fallback cooldown after HTTP 429 |
| `JACRED_MAX_QUEUE_WAIT_SECONDS` | `10` | Maximum wait for an upstream slot |
| `JACRED_CACHE_TTL_SECONDS` | `180` | Rendered-response cache lifetime |
| `JACRED_CACHE_MAX_ENTRIES` | `512` | Maximum rendered responses |
| `JACRED_SOURCE_CACHE_TTL_SECONDS` | `180` | Raw source-result cache lifetime |
| `JACRED_SOURCE_CACHE_MAX_ENTRIES` | `128` | Maximum raw source-result sets |
| `JACRED_LOG_REQUEST_PARAMS` | `0` | Optional sanitized request diagnostics; off by default |

The caches are process-local and are cleared when the service restarts. Environment settings can be provided through systemd service overrides.