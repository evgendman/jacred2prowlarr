# jacred2prowlarr

[English](README.md) | [Русский](README.ru.md)

**JacRed v2 JSON → Torznab adapter for Prowlarr, Sonarr and Radarr.**

Current version: **2.2.10**

The adapter turns JacRed's Russian-tracker release data into consistent Torznab results that the *Arr applications can parse and rank more reliably. It is intended to bridge two gaps: Russian release titles do not always follow the naming patterns expected by Sonarr/Radarr, and a direct indexer connectivity test may fail when the upstream returns no real result for an empty search.

## Why use an adapter?

Russian trackers often put essential details into free-form release names. A single title may mix a localized title, an original title, season/episode ranges, release source, resolution, video codec and dubbing information. That is useful to a human, but inconsistent naming can prevent automatic series/film matching or make a release's quality harder to evaluate.

Audio is especially important for Russian-language libraries: the preferred dub or voice-over may be present in the release metadata or title, while a generic source result does not always expose it in a form that can be used for automatic selection.

This adapter:

- converts the **JacRed v2 JSON API** into Torznab RSS/XML for Prowlarr;
- formats movies and TV releases with separate title-building rules;
- preserves the original JacRed release title while building a normalized title for *Arr matching;
- extracts available voice/dubbing labels and exposes them in the formatted release title;
- supplements some TV searches with a confidently detected Russian alias;
- returns local synthetic test items for supported empty-query connectivity checks, without sending those checks to JacRed;
- caches both raw source results and rendered Torznab responses, avoiding repeated upstream queries across clients and pagination;
- limits and serializes outgoing JacRed requests to reduce the risk of rate-limiting.

The adapter does **not** choose the final release itself. It makes the title and metadata easier for Sonarr/Radarr to understand; Quality Profiles and Custom Formats in those applications make the final decision.

## Data flow

```text
Sonarr / Radarr
       |
       v
    Prowlarr
       |
       v
JacRed TV + Movies adapter
  127.0.0.1:9128/torznab
       |
       v
JacRed v2 JSON API (jac.red)
```

Prowlarr sees the adapter as a Torznab indexer. The adapter listens on localhost and queries JacRed **only when it receives a real search request**; it does not poll the source in the background. One running adapter can serve Prowlarr searches originating from multiple Sonarr/Radarr instances, including separate 1080p and 4K instances, sharing in-memory caches and the same upstream request limiter.

## What the adapter returns

### Separate movie and TV formatting

The adapter has two formatting branches:

- **Movies:** original movie title, year, release/source format, detected resolution/quality, detected voice labels, then the original source title where it differs from the normalized title.
- **TV:** original series title, normalized season/episode notation, year, release/source format, detected resolution/quality, detected voice labels, then the original source title where it differs from the normalized title.

Examples of TV notation normalization include:

- a season-and-episode release such as `6 сезон (1-22 из 22)` → `S06E01-22`;
- an explicit episode range such as `S11E01-10` → `S11E01-10`;
- a multi-season collection such as `1-11 сезоны` or `Seasons 1-11` → `S01-S11`.

The parser gives explicit episode/range notation priority over a generic season marker. That prevents a multi-season pack for seasons 1–11 from being mislabeled as season 11 only.

### Example: a real X-Files multi-season release

A release title returned by JacRed can look like this:

```text
Секретные материалы (1-11 сезоны: 1-217 серии из 217) / The X-Files / 1993-2018 / ДБ (ТВ3), ПМ (ТВ3), СТ / HEVC / BDRip (1080p) | Дубляж
```

The adapter formats it as:

```text
X-Files S01-S11 (1993) BDRip 1080p | ТВ-3 *** Секретные материалы (1-11 сезоны: 1-217 серии из 217) / The X-Files / 1993-2018 / ДБ (ТВ3), ПМ (ТВ3), СТ / HEVC / BDRip (1080p) | Дубляж
```

The beginning contains a predictable series name, season range, year, source format, quality and a recognized voice label. The `***` separator preserves the original JacRed release name, including source-specific details that may not have been normalized. The original source title is also provided in the Torznab item's description.

For movies, the season/episode segment is omitted and the rest of the normalization follows a separate movie-specific path.

### Voice and language metadata

Available dubbing/voice information is taken, in order, from:

1. Russian audio-stream title tags in JacRed's `ffprobe` metadata;
2. JacRed's `info.voices` field;
3. supported voice-over notation parsed from the original release title.

Where detected, voice labels are appended to the normalized title (for example, `| ТВ-3`). The item also exposes available audio languages through Torznab language metadata. This gives Custom Formats a useful title signal for preferred dubs/voice-overs, alongside resolution, source format, HDR and other release attributes. Actual scoring and automatic selection are configured in Sonarr/Radarr.

### Quality, source and protocol metadata

Quality detection uses this priority:

1. video dimensions from `ffprobe`, when available;
2. JacRed `info.quality`;
3. an explicit resolution marker in the release title.

This retains useful quality information even when a Russian release title omits an explicit resolution token. Where applicable, 4K is formatted as `2160p HDR` if HDR/Dolby Vision markers are present. The adapter also emits resolution/video-codec attributes where ffprobe provides them.

Other protocol details include the standard `application/x-bittorrent` enclosure MIME type, magnet URL and infohash attributes, and date handling for JacRed RFC3339/RFC3339Nano timestamps. Malformed or absent source dates are not replaced with the current time. Duplicate merged results are removed by infohash where possible.

## Additional Russian-title search for TV

Sonarr commonly starts a search with the series' original/international title. Russian trackers may index the same series under its Russian title, and many release names place both forms in a title separated by `/`.

For a non-Cyrillic TV query, the adapter checks distinct source releases for repeated, matching title pairs. It only accepts an alias when there is evidence from multiple releases and the leading candidate has a clear majority among eligible matches. It then:

1. queries JacRed using the detected Russian alias;
2. keeps only alias-query results that also match the canonical series title;
3. merges those results with the original-query results;
4. deduplicates the combined set by infohash.

For example, a Sonarr query for **`X Files`** exposed the paired title **`Секретные материалы / The X-Files`** in multiple distinct JacRed releases. The adapter detected **`Секретные материалы`** as the Russian alias, queried JacRed again with that name and merged matching X-Files releases. The normalized series name remains **`X-Files`**; the alias search expands discovery rather than renaming the series in Sonarr.

This is an evidence-based TV-specific fallback, not a blind translation or an unconditional second query for every search.

## Torznab support and categories

The adapter supports:

- generic `search`;
- `tv-search`;
- `movie-search`.

Prowlarr indexer name suggestion: **JacRed TV + Movies**

Endpoint:

`http://127.0.0.1:9128/torznab`

The service also accepts the corresponding `/torznab/api` and `/api` paths.

Supported categories include:

- **TV:** parent category `5000`, including Anime subcategory `5070`;
- **Movies:** parent category `2000` and source categories `2010, 2020, 2030, 2040, 2045, 2050, 2060, 2070, 2080, 2090`.

Capabilities advertise the Movies and TV parents and the Anime subcategory. Returned items include correct parent categories, retain supported source subcategories when present, and expose Anime when applicable. If JacRed has only the movie parent category, a search requested under a supported movie subcategory can still return that movie; the adapter does not invent a more specific source category.

## Local connectivity tests for Prowlarr, Sonarr and Radarr

An indexer connectivity test is not necessarily a real title search. Some tests send an empty `q`; when the upstream answers with an empty feed, the direct source may fail the test despite the endpoint being reachable.

For a recognized empty-query connectivity check, the adapter returns a small **synthetic local test feed** suitable for the requested client/media type:

- movie for a movie/Radarr test;
- TV for a TV/Sonarr test;
- both for an unfiltered generic Prowlarr test;
- supported requested categories are reflected where appropriate.

These synthetic items are test responses, not real JacRed releases. That path never queries JacRed and includes a valid publication date for Torznab clients.

An empty query containing a title ID, year, season or episode is **not** treated as a connectivity test. Such structured requests return an empty feed rather than a fabricated title or an empty search to JacRed. This also avoids silently substituting a made-up query.

## Two-level in-memory caching

The adapter maintains two independent caches:

| Cache | Default lifetime | Maximum entries | What it stores |
|---|---:|---:|---|
| Raw source-result cache | 180 s | 128 | JacRed results keyed by query, year and media type |
| Rendered Torznab-response cache | 180 s | 512 | XML response keyed by request parameters that affect that response |

This distinction matters when several *Arr clients or quality tiers search for the same title:

- Different Torznab pages (for example, `offset=0` and `offset=100`) need different rendered responses but can reuse one raw JacRed result set.
- Local category, season and episode filtering does not require downloading the same source result set again.
- Identical concurrent requests are coalesced at both cache layers.
- API-key differences do not create separate rendered cache entries.
- Failed, rate-limited and partial upstream responses are not cached as successful complete results.

The caches are in memory and live for the lifetime of the service process; they are not persistent storage.

## Upstream request pacing and error handling

All outbound JacRed requests share one process-wide limiter:

- requests are serialized rather than sent in parallel;
- by default, the adapter observes a minimum **1000 ms quiet interval** between completed upstream requests and the next request;
- HTTP 429 activates a global cooldown and honors `Retry-After` when JacRed provides it;
- if a request would have to wait longer than the configured queue budget, the adapter fails fast with HTTP 503 and `Retry-After`, instead of holding the client connection until it times out;
- a successful TV or movie branch is preserved if the other branch of a generic search fails, while partial responses are not cached.

Default environment settings:

| Variable | Default | Purpose |
|---|---:|---|
| `JACRED_MIN_INTERVAL_MS` | `1000` | Minimum quiet interval between upstream requests |
| `JACRED_429_BACKOFF_SECONDS` | `60` | Fallback cooldown after HTTP 429 without a usable `Retry-After` |
| `JACRED_MAX_QUEUE_WAIT_SECONDS` | `10` | Maximum time allowed waiting for an upstream slot |
| `JACRED_CACHE_TTL_SECONDS` | `180` | Rendered-response cache lifetime |
| `JACRED_CACHE_MAX_ENTRIES` | `512` | Maximum rendered responses |
| `JACRED_SOURCE_CACHE_TTL_SECONDS` | `180` | Raw source-result cache lifetime |
| `JACRED_SOURCE_CACHE_MAX_ENTRIES` | `128` | Maximum raw source-result sets |
| `JACRED_LOG_REQUEST_PARAMS` | `0` | Opt-in request diagnostics; off by default |

Settings can be changed with systemd service environment overrides. Diagnostic query logging should be enabled only when troubleshooting; credential-like parameters are redacted.

## Deployment

The existing systemd unit is:

`jacred-v2-adapter.service`

Installed script:

`/opt/jacred-v2-adapter/jacred_v2_adapter.py`

The adapter binds to `127.0.0.1:9128`. Configure Prowlarr's Torznab indexer to use the endpoint above on the same machine.

See [ROADMAP.md](ROADMAP.md) for implementation history and remaining end-to-end validation work.