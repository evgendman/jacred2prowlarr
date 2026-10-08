# jacred2prowlarr

JacRed v2 JSON → Torznab adapter for Prowlarr.

## Current version

**2.2.2**

The adapter runs as the existing `jacred-v2-adapter.service` and listens on:

`http://127.0.0.1:9128/torznab`

Recommended Prowlarr indexer name:

**JacRed TV + Movies**

## Supported media

- TV: categories 5000 / 5070
- Movies: parent 2000 and movie subcategories 2010, 2020, 2030, 2040, 2045, 2050, 2060, 2070, 2080, 2090
- Torznab `tv-search`
- Torznab `movie-search`
- Generic `search`

## Prowlarr test protection

A generic Torznab `t=search` request without a real `q` is answered locally with a synthetic test release.

The synthetic release includes a valid `pubDate` required by Prowlarr/Torznab clients.

JacRed source dates in RFC3339/RFC3339Nano form are parsed correctly; malformed dates never fall back to the current time.

The adapter does **not** contact `jac.red` for this connectivity test, so repeated Prowlarr/Sonarr/Radarr tests do not consume JacRed requests.

## Request cache

Successful Torznab responses are cached in memory for **60 seconds**, with a maximum of **128** entries.

The cache key covers the query parameters that affect the adapter response. The API key itself is ignored so identical searches from different clients can share one response.

Concurrent identical requests use the same in-flight cache key, preventing duplicate upstream JacRed requests.

Failures and rate-limit responses are never cached as successful results.

## Quality

Quality detection priority:

1. ffprobe dimensions when available;
2. JacRed `info.quality`;
3. explicit resolution in the source title.

This preserves JacRed's 1080p information when the release title itself omits `1080p`.

4K HDR is represented as `2160p HDR` when HDR/Dolby Vision markers are available.

Torznab torrent enclosures use the standard `application/x-bittorrent` MIME type; the magnet URI remains in the enclosure URL and Torznab `magneturl` attribute.

## Deployment

The installed systemd service uses:

`/opt/jacred-v2-adapter/jacred_v2_adapter.py`

Service:

`jacred-v2-adapter.service`

See [ROADMAP.md](ROADMAP.md) for the project history and remaining validation work.

## Upstream error handling

When a generic search needs both TV and Movies, an upstream failure in one media type no longer discards successful results from the other type. Partial responses are not cached.
