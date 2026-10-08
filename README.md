# jacred2prowlarr

JacRed v2 JSON → Torznab adapter for Prowlarr.

## Current version

**2.1.0**

The adapter runs as the existing `jacred-v2-adapter.service` and listens on:

`http://127.0.0.1:9128/torznab`

## Supported media

- TV: categories 5000 / 5070
- Movies: parent 2000 and movie subcategories 2010, 2020, 2030, 2040, 2045, 2050, 2060, 2070, 2080, 2090
- Torznab `tv-search`
- Torznab `movie-search`
- Generic `search`

## Quality

Quality detection priority:

1. ffprobe dimensions when available;
2. JacRed `info.quality`;
3. explicit resolution in the source title.

This preserves JacRed's 1080p information when the release title itself omits `1080p`.

4K HDR is represented as `2160p HDR` when HDR/Dolby Vision markers are available.

## Deployment

The installed systemd service uses:

`/opt/jacred-v2-adapter/jacred_v2_adapter.py`

Service:

`jacred-v2-adapter.service`
