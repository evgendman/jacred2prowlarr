# HDR title normalization

## Scope

JacRed's current JSON result schema exposes a generic `info.videotype` value (`hdr` or `sdr`) but does not expose enough consistent metadata to distinguish HDR10, HDR10+ and Dolby Vision. The adapter therefore normalizes labels from the original release title only.

These are **title-derived hints**, not a claim that the encoded video stream was independently inspected. Do not infer a specific HDR standard from codec, resolution, bit depth or `info.videotype`.

## Canonical labels

| Label in normalized title | Source-title evidence |
|---|---|
| `HDR10` | Explicit `HDR10` token that is not the prefix of `HDR10+`, or an explicit Dolby Vision Profile 8.1 marker establishing an HDR10-compatible base layer |
| `HDR10+` | Explicit `HDR10+` or `HDR10 Plus` token |
| `Dolby Vision` | Explicit `Dolby Vision` or `DoVi`; a `DV` token only with a profile/version or a same-title HDR/UHD clue |
| `HDR` | Generic `HDR` / `HDR+` token when no more specific HDR type is named |
| `SDR` | Explicit `SDR` token, only when no HDR marker is found |

Markers are emitted in canonical order: `HDR10`, `HDR10+`, `Dolby Vision`. Generic `HDR` does not get added redundantly when a specific HDR type is present. A bare `DV` without context is ignored to reduce false positives; examples of recognized context include `DV 8.1`, `DV P8`, `HDR10 DV` and `4K DV`. A bare `HDR+` is not interpreted as `HDR10+`.

## Placement in normalized titles

Markers are appended to the normalized quality segment at any resolution, for example:

- `Lanterns S01E01 (2026) WEB-DL 2160p HDR10+ Dolby Vision`
- `Lanterns S01E01 (2026) WEB-DL 1080p HDR10+`
- `Example Movie (2024) WEB-DL 2160p HDR10`
- `Example Movie (2024) WEB-DL 1080p SDR`

If quality cannot be established, title-derived labels are still retained. The original JacRed title is preserved after `***`, so the source wording remains available for inspection.

## Operational implications

- Sonarr/Radarr Custom Formats can match stable, canonical tokens in the normalized title.
- A release explicitly labelled `HDR10+ Dolby Vision` retains both labels; the adapter does not add a second standard that is absent from the source title.
- An `info.videotype=hdr` value with no HDR marker in `Title` does not cause an HDR label to be synthesized.
- An explicit Profile 8.1 title marker (`DV 8.1`, `DV Profile 8.1`, `DoVi P8.1`, or `Dolby Vision Profile 8.1`) adds `HDR10` as a compatibility marker if missing while retaining `Dolby Vision`. Bare `DV`, unspecified Profile 8, and other profiles do not trigger this inference.
- The presence of a marker does not prove the file contains that format. The source may be mislabeled; title-only classification intentionally preserves the publisher/releaser claim without pretending to validate the stream.
