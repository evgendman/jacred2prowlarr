# Roadmap / Implementation status

[English documentation](README.md) | [Документация на русском](README.ru.md)

Current release on MINI-Ubuntu: **2.4.0** (installed and validated 2026-10-14); source commit: [`bcaf416d83b86041cd0049da90dddb33c53e1bb4`](https://github.com/evgendman/jacred2prowlarr/commit/bcaf416d83b86041cd0049da90dddb33c53e1bb4); [CI run #30 passed](https://github.com/evgendman/jacred2prowlarr/actions/runs/38595960157) (Python compilation and all 32 tests).

## Project goal

Provide one local Torznab endpoint for Prowlarr that converts JacRed v2 JSON results into predictable movie and TV release records for Sonarr and Radarr. The adapter addresses Russian-tracker naming, dubbing/voice metadata, category reporting, empty-query connectivity tests, duplicate upstream traffic and request pacing.

This is a metadata/search adapter: the final automatic release decision remains the responsibility of Sonarr/Radarr Quality Profiles and Custom Formats.

## JacRed API versions and endpoint inventory

- The public `https://jac.red/version` endpoint reported **3.15.0**, Git SHA `f43eeba8`, build date `2026-09-18 14:08:55 UTC`, on 2026-10-12. The matching route inventory is documented in [docs/jacred-api-endpoints.md](docs/jacred-api-endpoints.md) and [the Russian version](docs/jacred-api-endpoints.ru.md).
- The adapter's configured default upstream is local: `http://127.0.0.1:9117/api/v2.0/indexers/all/results`. The public endpoint remains historical incident context, not the configured production source.
- Planned local integration-test target: upstream release/tag [`3.18.0-oss`](https://github.com/jacred-fdb/jacred/releases/tag/3.18.0-oss), using the locally deployed instance documented in [evgendman/jacred-sync-node](https://github.com/evgendman/jacred-sync-node).
- Before tests, capture the local instance's actual `/version` response. Do not mark compatibility with `3.18.0-oss` as verified unless that exact version is running and the tests have passed. Record schema/behavior differences from the public `3.15.0` baseline.
- Keep route inventory and live reachability separate: success at `/`, `/version` or Torznab `caps` does not establish that the adapter's JSON search route is accessible.

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
- **v2.4.0 live validation (2026-10-14):** CI run [#30](https://github.com/evgendman/jacred2prowlarr/actions/runs/38595960157) passed Python compilation and all 32 tests. On MINI-Ubuntu, the v2.4.0 service is active, startup logs confirm the local JacRed upstream at `127.0.0.1:9117` and a 1000 ms minimum interval, and Torznab `t=caps` returns HTTP 200. A live `tvsearch` for `Lanterns` returned HTTP 200 and 100 releases; 29 returned normalized HDR/SDR/Dolby Vision markers. The user confirmed searches and the relevant behavior work in both Sonarr and Radarr.

## Remaining end-to-end validation

The v2.4.0 release and its HDR-specific acceptance checks are complete. The items below are additional coverage for the wider deployment, not blockers for the verified v2.4.0 behavior.

1. **Complete the full configured-client matrix.** A real search and the relevant HDR behavior have been verified by the user in Sonarr and Radarr. The roadmap does not yet record an explicit per-instance check of every configured path:
   - Prowlarr;
   - Sonarr 1080p and 4K;
   - Radarr 1080p and 4K.
2. **Cross-client/cache regression.** For the same title searched by multiple clients and quality tiers, verify that the shared raw-source cache avoids redundant identical JacRed queries while distinct local filters still produce correct independent result pages.
3. **Combined automatic selection.** HDR title normalization and the relevant Sonarr/Radarr behavior have been checked. The broader combined ranking path still needs an explicitly recorded end-to-end case covering preferred voice/dubbing scores together with resolution, source format and HDR. Scoring policy belongs to Sonarr/Radarr.
4. **Remaining mock-upstream failure cases.** The automated suite already covers HTTP 429 and both `Retry-After` formats, HTTP 403/401, persisted protection state, single HALF_OPEN probing, cache behavior while OPEN, serialized requests and a combined-search abort on 403. Add explicit local-mock cases for queue-wait expiration/HTTP 503, upstream timeouts, and partial movie/TV branch failures. Do not use public JacRed to generate failure cases. Any real-source recovery check must be a single controlled attempt after the protection period.

## v2.4.0: title-only HDR label normalization

The local JacRed sample query for `Lanterns` (2026) returned 106 unique releases. `info.videotype` was present for all 106 items, but only as a generic `hdr`/`sdr` value; `info.video_type` was absent. The 76 releases with ffprobe stream metadata did not expose the fields needed here to distinguish HDR subtypes (such as `color_primaries`, `color_transfer`, `pix_fmt`, or `side_data_list`). We therefore do not treat the current API fields as evidence for HDR10, HDR10+ or Dolby Vision.

Version 2.4.0 deliberately bases HDR/SDR label normalization **only on the original JacRed `Title`**. It does not infer the specific standard from `info.videotype`, codec, resolution or bit depth.

- [x] Detect explicit title tokens for `HDR10`, `HDR10+`, `Dolby Vision` / `DoVi`, generic `HDR` and `SDR`.
- [x] Interpret a bare `DV` conservatively: accept it only with a profile/version (for example `DV 8.1`) or another HDR/UHD clue in the same title; ignore an unqualified `DV`.
- [x] Normalize specific markers into a stable order and preserve combined explicit markers, e.g. `HDR10+ Dolby Vision`.
- [x] Append title-derived markers to normalized quality at all resolutions, not only 2160p, while continuing to preserve the original source title after `***`.
- [x] Add fixture-style unit tests for label parsing, ambiguous `DV`, SDR, missing structured evidence, and normalized output titles.
- [x] Deployment and integration: representative output was verified through the local Torznab endpoint; the `Lanterns` query returned HTTP 200 and included normalized `HDR10+`, `Dolby Vision`, generic `HDR` and `SDR` labels. The user confirmed the relevant search/Custom Format behavior works in Sonarr and Radarr.

The labels remain **title-derived hints**, not claims that the video stream has been independently inspected. A generic `HDR` label is emitted only when the title does not specify a more concrete HDR type; `HDR+` alone is not promoted to `HDR10+`. Details and examples: [English policy](docs/hdr-title-normalization.md) and [Russian policy](docs/hdr-title-normalization.ru.md).


## Planned: source-only HDR metadata enrichment in the normalized title

**Goal:** supplement the adapter's normalized release title with missing HDR-format labels, using only the JacRed search result already returned for that release. Do not download a torrent or video, run another `ffprobe`, or perform frame-level analysis.

### Evidence and classification

- Continue using the original JacRed `Title` as the existing fallback for release-label parsing.
- Inspect structured `ffprobe` fields only when they are already present in the current JacRed result. Do not assume optional fields exist; the currently sampled API responses included generic `info.videotype=hdr/sdr` but did not expose the fields needed to distinguish HDR subtypes reliably.
- Do not infer a specific standard from `info.videotype`, codec, resolution or bit depth alone. If relevant evidence is missing or ambiguous, retain the current title-derived behavior and do not invent a format label.
- When structured Dolby Vision configuration is already available, use the reported profile and base-layer compatibility identifier where present; never treat every DV profile as HDR10-compatible.
- **Profile 8.1 title rule:** when the original release title explicitly identifies Dolby Vision Profile 8.1 (for example, `DV 8.1` or `Dolby Vision 8.1`), infer HDR10 compatibility and add `HDR10` to the normalized title if it is missing. Do not apply this inference to a bare `DV`, an unspecified Profile 8, or another DV profile without sufficient evidence.
- Detect `HDR10+` from explicit title evidence or already-present, explicit HDR10+ metadata. Do not infer it from generic HDR or HDR10. Do not add a separate `HDR10` marker solely because HDR10+ is backward-compatible with HDR10.

### Normalized-title output

- Append missing canonical markers to the **normalized title's quality segment**, after source/quality and resolution tokens where present, using the existing canonical order: `HDR10`, `HDR10+`, `Dolby Vision`.
- Recognize existing labels case-insensitively and account for equivalent spellings such as `DoVi`; avoid duplicate labels.
- Do not rewrite or remove source-title text. Preserve the original JacRed `Title` after the existing `***` separator and in the Torznab description.
- Example: source title `Example Movie 2160p DV 8.1` → normalized title includes `2160p HDR10 Dolby Vision`, while the original source title remains intact after `***`.
- These labels describe evidence available in the source metadata or release name; they must not be presented as proof that the file was independently inspected or will play correctly on every device.

### Acceptance tests

- [ ] Explicit `DV 8.1` / `Dolby Vision 8.1` without `HDR10` adds the missing `HDR10` marker to the normalized title.
- [ ] Already-present HDR labels are not duplicated, and the original JacRed title remains unchanged after `***`.
- [ ] Explicit HDR10+ is normalized as `HDR10+` without adding a redundant `HDR10` marker solely from compatibility.
- [ ] A bare or ambiguous `DV`, an unspecified Profile 8, and Profile 5 do not trigger the Profile 8.1 HDR10 inference.
- [ ] Missing or insufficient structured metadata does not trigger additional video/file analysis and does not create unsupported labels.
- [ ] Existing movie and TV title-formatting paths, all resolutions, and the current title-only HDR behavior remain covered by tests.

## v2.3.0: local source and persistent upstream protection

**v2.3.0 baseline validation as of 2026-10-13:** GitHub Actions run #12 passed Python compilation and all 17 mock-upstream tests on commit `025a0a57911cb5bd06d8c1c85a7c3425be8e5b79`. This validation applies to the 2.3.0 baseline; the 2.3.1 interval change requires its own CI run.

- The local-node smoke test was also completed manually: JacRed reports `3.18.0-oss`, Git SHA `c207d0d4`; `/version` and `/api/v2.0/indexers/all/results` returned HTTP 200, and the search response contained `Results`, release metadata and five ffprobe streams.
- Baseline 2.3.0 checks and the 2.3.1 deployment were completed on MINI-Ubuntu on 2026-10-13. Version 2.3.1 was installed from CI-verified commit `a1e7f30f14e75095cef15b1e5e7b73721b837b15`; the service is `active` and `enabled`; startup logs confirm the local source at `127.0.0.1:9117` and `minimum interval=1000ms`. Torznab caps and synthetic tests returned HTTP 200. A post-upgrade real movie search returned valid releases with magnet URLs and infohashes, and the user confirmed searches work in both Sonarr and Radarr.
- The automated suite covers syntax, request shape/User-Agent, serialized traffic, Retry-After parsing, 429/403/401 protection, persistent state, single HALF_OPEN probe, cache use while OPEN, and local synthetic connectivity responses. It does not replace the remaining deployment-level checks.

- Default source is now `http://127.0.0.1:9117/api/v2.0/indexers/all/results`, the local JacRed instance documented in [jacred-sync-node](https://github.com/evgendman/jacred-sync-node). `JACRED_URL` keeps the adapter portable to any compatible JacRed v2 instance.
- The default User-Agent is a stable Chrome 111 value aligned with the upstream client's inspected HTTP client; `JACRED_USER_AGENT` can override it. Do not use header changes to bypass an active restriction.
- `JACRED_MIN_INTERVAL_MS` defaults to 5000 ms and is clamped to a hard 5000 ms minimum. Pacing is measured from completion of the prior upstream attempt.
- SQLite stores circuit state, cooldown expiry, failure counters and gradual-recovery progress at `JACRED_STATE_PATH` (default `/var/lib/jacred-v2-adapter/upstream_state.sqlite3`). The service account must be able to write this path.
- HTTP 429 opens the circuit with escalating delays (60 s, 5 min, 15 min, 1 h, 6 h) and honors the longer of configured delay and source `Retry-After`.
- First HTTP 403 opens a 24-hour safety block. A 403 on the eligible controlled probe opens a seven-day block and sets a persistent manual-review flag; expiry alone does not permit another probe. HTTP 401 requires manual intervention.
- After an eligible successful probe, recovery starts at 60 seconds and advances through 30, 15, 10 and 5 seconds after three consecutive successful requests per stage. Escalation clears only after the configured stable-success count.
- The local synthetic Torznab connectivity path remains source-free. Existing response and raw-result caches remain in place.

### v2.3.0 configuration (deployed baseline; 5000 ms default)

| Variable | Default | Purpose |
|---|---|---|
| `JACRED_URL` | `http://127.0.0.1:9117/api/v2.0/indexers/all/results` | JacRed v2 JSON endpoint; override for another compatible node |
| `JACRED_USER_AGENT` | Stable Chrome 111 UA | Fixed upstream HTTP User-Agent |
| `JACRED_MIN_INTERVAL_MS` | `5000` | Minimum quiet interval; lower values are clamped to 5000 ms |
| `JACRED_MAX_QUEUE_WAIT_SECONDS` | `10` | Queue wait budget; exceeding it returns local HTTP 503 |
| `JACRED_STATE_PATH` | `/var/lib/jacred-v2-adapter/upstream_state.sqlite3` | Durable SQLite protection state |
| `JACRED_429_BACKOFF_SECONDS` | `60` | Legacy-compatible fallback for malformed/missing Retry-After |
| `JACRED_429_BACKOFF_STEPS_SECONDS` | `60,300,900,3600,21600` | Escalating 429 delays |
| `JACRED_RECOVERY_INTERVALS_SECONDS` | `60,30,15,10,5` | Gradual-recovery intervals; values are never below five seconds |
| `JACRED_RECOVERY_SUCCESS_COUNT` | `3` | Consecutive successes per recovery step |
| `JACRED_STABLE_SUCCESS_COUNT` | `15` | Stable successes before clearing failure escalation |
| `JACRED_PROBE_FAILURE_SECONDS` | `300` | Temporary block after a failed non-403 recovery probe |
| `JACRED_CACHE_TTL_SECONDS` | `180` | Rendered-response cache lifetime |
| `JACRED_CACHE_MAX_ENTRIES` | `512` | Maximum rendered responses |
| `JACRED_SOURCE_CACHE_TTL_SECONDS` | `180` | Raw source-result cache lifetime |
| `JACRED_SOURCE_CACHE_MAX_ENTRIES` | `128` | Maximum raw source-result sets |
| `JACRED_LOG_REQUEST_PARAMS` | `0` | Optional sanitized query diagnostics |

### v2.3.1: relax steady-state pacing to one request per second

- Lower `MIN_UPSTREAM_INTERVAL_FLOOR_MS` from 5000 ms to 1000 ms, and change the `JACRED_MIN_INTERVAL_MS` default from 5000 to 1000.
- The global limiter still serializes upstream calls and measures the quiet interval from completion of one request to the start of the next. In steady state, the configured floor therefore permits at most one new upstream request per second; concurrent clients do not bypass the shared limiter.
- This is a minimal pacing change only. HTTP 429/403/401 circuit-breaker behavior, persisted SQLite state, Retry-After handling, cooldowns and manual review rules remain unchanged.
- The controlled recovery schedule remains `60, 30, 15, 10, 5` seconds. After an upstream incident, the adapter still restores request speed gradually rather than jumping straight to the steady-state 1-second floor.
- The installed systemd drop-in must set `JACRED_MIN_INTERVAL_MS=1000`; changing the code default alone would not override an explicit service environment value of 5000.

### v2.3.1 configuration

| Variable | Default | Purpose |
|---|---|---|
| `JACRED_MIN_INTERVAL_MS` | `1000` | Minimum quiet interval; values below 1000 ms are clamped to 1000 ms |
| `JACRED_RECOVERY_INTERVALS_SECONDS` | `60,30,15,10,5` | Recovery after a protected upstream incident; left unchanged in this patch |

Manual reset (only after reviewing the source status and configuration):

```bash
sudo systemctl stop jacred-v2-adapter.service
sudo -u jacred-v2-adapter /opt/jacred-v2-adapter/venv/bin/python /opt/jacred-v2-adapter/jacred_v2_adapter.py --reset-upstream-protection
sudo systemctl start jacred-v2-adapter.service
```

Adjust the service account and interpreter path to match the actual unit. Resetting protection does not itself test source reachability.

### v2.3.1 deployment validation — completed

- [x] CI: Python compilation and the full mock-upstream test job passed for the v2.3.1 release source commit `a1e7f30f14e75095cef15b1e5e7b73721b837b15`.
- [x] Runtime: installed service is `active` and `enabled`; startup confirms `v2.3.1` and `minimum interval=1000ms`.
- [x] Config: `JACRED_URL` remains the local JSON endpoint at `127.0.0.1:9117`; `JACRED_STATE_PATH` and the existing SQLite state were preserved.
- [x] Torznab: capabilities and synthetic movie test return HTTP 200.
- [x] Real source: post-upgrade movie search returned valid releases including magnet URLs and infohashes.
- [x] Arr clients: the user confirmed real searches continue to work in Sonarr and Radarr.
- The steady-state minimum is 1000 ms; HTTP 403/429/401 protection, serialized upstream access, SQLite persistence and gradual recovery remain in place. Do not send deliberate failure probes to public JacRed.

## Implemented behavior

### Media types, categories and Torznab

- One existing systemd service was extended from TV-only to movies and TV; endpoint: `http://127.0.0.1:9128/torznab`.
- Supports generic `search`, `tv-search` and `movie-search`.
- TV parent category `5000` and Anime subcategory `5070`; movie parent `2000` and supported movie source categories `2010, 2020, 2030, 2040, 2045, 2050, 2060, 2070, 2080, 2090`.
- Movie and TV records use separate title-formatting branches.
- Movie subcategory filtering does not discard a release merely because JacRed only supplied its parent category; unsupported source details are not invented.
- Quality detection priority: ffprobe dimensions → JacRed `info.quality` → release title. Video resolution/codec metadata may be exposed when available. v2.4.0 normalizes title-derived `HDR10`, `HDR10+`, `Dolby Vision` / `DoVi`, generic `HDR` and `SDR` markers into the normalized title. A further planned improvement to infer HDR10 compatibility from an explicitly named Dolby Vision Profile 8.1 is tracked below; these remain title-derived or source-metadata-derived hints, not independent verification of video playback.
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

This table is retained as a **historical v2.2.10 baseline**; it does not describe the current v2.3.x implementation. The current request pacing and persistent upstream-protection behavior are documented in the v2.3.x sections above.

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