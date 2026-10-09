# Roadmap / Implementation status

[English documentation](README.md) | [Документация на русском](README.ru.md)

Current adapter version: **2.2.10**

## Project goal

Provide one local Torznab endpoint for Prowlarr that converts JacRed v2 JSON results into predictable movie and TV release records for Sonarr and Radarr. The adapter addresses Russian-tracker naming, dubbing/voice metadata, category reporting, empty-query connectivity tests, duplicate upstream traffic and request pacing.

This is a metadata/search adapter: the final automatic release decision remains the responsibility of Sonarr/Radarr Quality Profiles and Custom Formats.

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
4. Observe normal operation for rate-limit handling and failure cases: a JacRed HTTP 429, a long busy queue, and a failure of only one branch of a generic movie + TV search. No upstream failure should be cached as a complete successful response.

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