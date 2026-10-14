#!/usr/bin/env python3
"""Collect a sanitized JacRed sample for HDR metadata analysis.

The collector makes exactly one request to a loopback JacRed endpoint. It never
downloads torrent content and removes magnet URLs, Details URLs, and other
URL/credential-like fields before writing the dataset. An infohash may be kept
to identify duplicates without retaining a link.
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCRIPT_VERSION = "1.0.0"
DEFAULT_SOURCE_URL = os.environ.get(
    "JACRED_URL",
    "http://127.0.0.1:9117/api/v2.0/indexers/all/results",
)
DEFAULT_QUERY = "Lanterns"
DEFAULT_YEAR = "2026"
DEFAULT_CATEGORY = "tv_"
DEFAULT_LIMIT = 1000
DEFAULT_TIMEOUT = 45

SENSITIVE_KEY_RE = re.compile(
    r"(?:magnet|url|uri|link|href|details?|api[_-]?key|token|password|secret|authorization|cookie)",
    re.IGNORECASE,
)
INFOHASH_HEX_RE = re.compile(r"^[0-9a-fA-F]{40}$")
INFOHASH_BASE32_RE = re.compile(r"^[A-Z2-7a-z2-7]{32}$")

HDR10_PLUS_RE = re.compile(r"\bhdr[\s._-]*10(?:\s*\+|\s*plus)", re.IGNORECASE)
HDR10_RE = re.compile(r"\bhdr[\s._-]*10\b", re.IGNORECASE)
HDR_GENERIC_RE = re.compile(r"\bhdr\b", re.IGNORECASE)
DOLBY_VISION_RE = re.compile(r"\bdolby[\s._-]*vision\b|\bdovi\b", re.IGNORECASE)
DV_TOKEN_RE = re.compile(r"\bdv\b", re.IGNORECASE)
SDR_RE = re.compile(r"\bsdr\b", re.IGNORECASE)

VIDEO_FIELDS = (
    "codec_type", "codec_name", "profile", "width", "height", "pix_fmt",
    "color_space", "color_transfer", "color_primaries", "chroma_location",
    "bits_per_raw_sample", "bits_per_coded_sample", "r_frame_rate",
    "avg_frame_rate", "side_data_list",
)
INFO_HDR_FIELDS = (
    "videotype", "video_type", "quality", "types", "name", "originalname",
)


def json_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    return type(value).__name__


def is_loopback_url(url: str) -> bool:
    """Allow only local JacRed endpoints, so this tool cannot probe public APIs."""
    try:
        parsed = urllib.parse.urlsplit(url)
        hostname = (parsed.hostname or "").lower().rstrip(".")
        return (
            parsed.scheme.lower() in {"http", "https"}
            and hostname in {"127.0.0.1", "::1", "localhost"}
            and parsed.username is None
            and parsed.password is None
        )
    except (TypeError, ValueError):
        return False


def redact_key(key: str) -> bool:
    return bool(SENSITIVE_KEY_RE.search(key))


def note_redaction(redactions: dict[str, dict[str, Any]], path: str, value: Any) -> None:
    item = redactions.setdefault(path, {"occurrences": 0, "types": set()})
    item["occurrences"] += 1
    item["types"].add(json_type(value))


def sanitize_value(
    value: Any,
    path: str = "",
    redactions: dict[str, dict[str, Any]] | None = None,
) -> Any:
    """Remove URLs, magnet links and credential-like fields recursively."""
    if redactions is None:
        redactions = {}
    if isinstance(value, dict):
        cleaned = {}
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else str(key)
            if redact_key(str(key)):
                note_redaction(redactions, child_path, child)
                continue
            cleaned[key] = sanitize_value(child, child_path, redactions)
        return cleaned
    if isinstance(value, list):
        return [sanitize_value(child, f"{path}[]", redactions) for child in value]
    return value


def normalize_infohash(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    value = value.strip()
    if INFOHASH_HEX_RE.fullmatch(value):
        return value.lower()
    if INFOHASH_BASE32_RE.fullmatch(value):
        return value.upper()
    return ""


def extract_infohash(item: dict[str, Any]) -> str:
    """Extract a BTIH hash but never save its magnet URI."""
    for key in ("MagnetUri", "magnet", "magnet_uri", "MagnetURI"):
        magnet = item.get(key)
        if not isinstance(magnet, str):
            continue
        parsed = urllib.parse.urlsplit(magnet)
        query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        for xt in query.get("xt", []):
            if xt.lower().startswith("urn:btih:"):
                normalized = normalize_infohash(xt[len("urn:btih:"):])
                if normalized:
                    return normalized
    for key in ("infohash", "InfoHash", "Infohash"):
        normalized = normalize_infohash(item.get(key))
        if normalized:
            return normalized
    return ""


def stable_sample_id(cleaned_item: dict[str, Any], infohash: str) -> str:
    if infohash:
        return infohash
    encoded = json.dumps(
        cleaned_item, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:20]


def make_sanitized_release(
    item: dict[str, Any],
    index: int,
    redactions: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    infohash = extract_infohash(item)
    cleaned = sanitize_value(item, redactions=redactions)
    if not isinstance(cleaned, dict):
        cleaned = {}
    cleaned["sample_index"] = index
    cleaned["sample_id"] = stable_sample_id(cleaned, infohash)
    if infohash:
        cleaned["infohash"] = infohash
    return cleaned


def walk_field_schema(
    value: Any,
    path: str,
    stats: dict[str, dict[str, Any]],
    redactions: dict[str, dict[str, Any]],
) -> None:
    if path:
        record = stats.setdefault(
            path,
            {
                "value_occurrences": 0,
                "types": set(),
                "samples": [],
                "_sample_set": set(),
                "_release_indexes": set(),
            },
        )
        record["value_occurrences"] += 1
        record["types"].add(json_type(value))
        if isinstance(value, (str, int, float, bool)) or value is None:
            sample = value
            if isinstance(sample, str):
                sample = sample.replace("\n", " ").replace("\r", " ")[:120]
            sample_key = json.dumps(sample, ensure_ascii=False, sort_keys=True)
            if sample_key not in record["_sample_set"] and len(record["samples"]) < 5:
                record["_sample_set"].add(sample_key)
                record["samples"].append(sample)

    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else str(key)
            if redact_key(str(key)):
                note_redaction(redactions, child_path, child)
                continue
            walk_field_schema(child, child_path, stats, redactions)
    elif isinstance(value, list):
        for child in value:
            list_path = f"{path}[]" if path else "[]"
            walk_field_schema(child, list_path, stats, redactions)


def build_field_schema(records: list[dict[str, Any]]) -> dict[str, Any]:
    stats: dict[str, dict[str, Any]] = {}
    redactions: dict[str, dict[str, Any]] = {}

    for release_index, item in enumerate(records):
        local: dict[str, dict[str, Any]] = {}
        walk_field_schema(item, "", local, redactions)
        for path, record in local.items():
            target = stats.setdefault(
                path,
                {
                    "value_occurrences": 0,
                    "types": set(),
                    "samples": [],
                    "_sample_set": set(),
                    "_release_indexes": set(),
                },
            )
            target["value_occurrences"] += record["value_occurrences"]
            target["types"].update(record["types"])
            target["_release_indexes"].add(release_index)
            for sample in record["samples"]:
                sample_key = json.dumps(sample, ensure_ascii=False, sort_keys=True)
                if sample_key not in target["_sample_set"] and len(target["samples"]) < 5:
                    target["_sample_set"].add(sample_key)
                    target["samples"].append(sample)

    finalized = {}
    for path, record in sorted(stats.items()):
        present = len(record["_release_indexes"])
        finalized[path] = {
            "release_count": present,
            "release_percent": round(100 * present / max(1, len(records)), 1),
            "value_occurrences": record["value_occurrences"],
            "types": sorted(record["types"]),
            "samples": record["samples"],
        }
    redacted_final = {
        path: {"occurrences": value["occurrences"], "types": sorted(value["types"])}
        for path, value in sorted(redactions.items())
    }
    return {
        "release_count": len(records),
        "fields": finalized,
        "redacted_fields": redacted_final,
    }


def detect_hdr_markers(text: Any) -> list[str]:
    """Return title/text candidates, not a claim about the actual video stream."""
    if not isinstance(text, str):
        return []
    markers = []
    if DOLBY_VISION_RE.search(text):
        markers.append("Dolby Vision / DoVi text")
    if HDR10_PLUS_RE.search(text):
        markers.append("HDR10+ text")
    elif HDR10_RE.search(text):
        markers.append("HDR10 text")
    if HDR_GENERIC_RE.search(text):
        markers.append("generic HDR text")
    if SDR_RE.search(text):
        markers.append("SDR text")
    if DV_TOKEN_RE.search(text):
        markers.append("ambiguous DV token")
    return markers


def find_video_streams(ffprobe: Any) -> list[dict[str, Any]]:
    streams = ffprobe.get("streams", []) if isinstance(ffprobe, dict) else ffprobe
    if not isinstance(streams, list):
        return []
    return [
        stream for stream in streams
        if isinstance(stream, dict) and stream.get("codec_type") == "video"
    ]


def make_hdr_observation(record: dict[str, Any]) -> dict[str, Any]:
    info = record.get("info")
    if not isinstance(info, dict):
        info = {}
    title = record.get("Title") if isinstance(record.get("Title"), str) else ""
    info_fields = {key: info[key] for key in INFO_HDR_FIELDS if key in info}
    info_text = " ".join(
        value if isinstance(value, str) else " ".join(map(str, value)) if isinstance(value, list) else ""
        for key, value in info_fields.items()
        if key in {"videotype", "video_type", "quality", "types"}
    )
    video_streams = [
        {key: stream[key] for key in VIDEO_FIELDS if key in stream}
        for stream in find_video_streams(record.get("ffprobe"))
    ]
    return {
        "sample_index": record.get("sample_index"),
        "sample_id": record.get("sample_id"),
        "infohash": record.get("infohash"),
        "title": title,
        "title_markers": detect_hdr_markers(title),
        "info_fields": info_fields,
        "info_markers": detect_hdr_markers(info_text),
        "video_stream_count": len(video_streams),
        "video_streams": video_streams,
    }


def dotted_get(record: dict[str, Any], path: str) -> Any:
    value: Any = record
    for component in path.split("."):
        if not isinstance(value, dict) or component not in value:
            return None
        value = value[component]
    return value


def summarize_value_distribution(
    records: list[dict[str, Any]], path: str, limit: int = 12
) -> dict[str, Any]:
    counts: collections.Counter[str] = collections.Counter()
    present = 0
    for record in records:
        value = dotted_get(record, path)
        if value is None:
            continue
        present += 1
        values = value if isinstance(value, list) else [value]
        for item in values:
            counts[json.dumps(item, ensure_ascii=False, sort_keys=True)] += 1
    return {
        "present_in_releases": present,
        "present_percent": round(100 * present / max(1, len(records)), 1),
        "top_values": [
            {"value": json.loads(value), "count": count}
            for value, count in counts.most_common(limit)
        ],
    }


def build_summary(
    args: argparse.Namespace,
    response: dict[str, Any],
    response_bytes: int,
    records: list[dict[str, Any]],
    sanitized_records: list[dict[str, Any]],
    schema: dict[str, Any],
    observations: list[dict[str, Any]],
) -> dict[str, Any]:
    hashes = [record.get("infohash") for record in sanitized_records if record.get("infohash")]
    marker_counts: collections.Counter[str] = collections.Counter()
    for observation in observations:
        marker_counts.update(observation["title_markers"])
        marker_counts.update(f"info: {marker}" for marker in observation["info_markers"])
    info_distributions = {
        f"info.{key}": summarize_value_distribution(records, f"info.{key}")
        for key in INFO_HDR_FIELDS
    }
    redacted_path_counts = {
        path: item["occurrences"]
        for path, item in schema["redacted_fields"].items()
    }
    return {
        "collector_version": SCRIPT_VERSION,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "query": {
            "q": args.query,
            "year": args.year,
            "category": args.category,
            "limit": args.limit,
        },
        "source": {
            "endpoint": "local loopback JacRed JSON API",
            "configured_endpoint": args.source_url.split("?", 1)[0],
            "http_status": 200,
            "response_bytes": response_bytes,
            "top_level_fields": sorted(response.keys()),
            "jacred_metadata": sanitize_value(response.get("jacred")),
        },
        "results_count": len(records),
        "unique_infohash_count": len(set(hashes)),
        "records_without_infohash": sum(not record.get("infohash") for record in sanitized_records),
        "info_field_distributions": info_distributions,
        "title_and_info_marker_counts": dict(sorted(marker_counts.items())),
        "video_streams_found": sum(obs["video_stream_count"] > 0 for obs in observations),
        "video_stream_count_total": sum(obs["video_stream_count"] for obs in observations),
        "redacted_field_occurrences": redacted_path_counts,
        "field_schema_path_count": len(schema["fields"]),
    }


def markdown_summary(summary: dict[str, Any], schema: dict[str, Any]) -> str:
    lines = [
        "# HDR metadata collection summary",
        "",
        f"- Collector version: {summary['collector_version']}",
        f"- Collected at (UTC): {summary['created_utc']}",
        f"- Query: {summary['query']['q']}",
        f"- Year: {summary['query']['year']}",
        f"- Category: {summary['query']['category']}",
        f"- Requested limit: {summary['query']['limit']}",
        f"- HTTP status: {summary['source']['http_status']}",
        f"- Results: {summary['results_count']}",
        f"- Unique infohashes: {summary['unique_infohash_count']}",
        f"- Releases without extractable infohash: {summary['records_without_infohash']}",
        f"- Releases with video streams: {summary['video_streams_found']}",
        f"- Video streams found: {summary['video_stream_count_total']}",
        f"- Field paths profiled: {summary['field_schema_path_count']}",
        "",
        "## HDR-like text markers in titles and selected info fields",
        "",
    ]
    if summary["title_and_info_marker_counts"]:
        for marker, count in summary["title_and_info_marker_counts"].items():
            lines.append(f"- {marker}: {count}")
    else:
        lines.append("- No recognized text markers found in this result set.")
    lines.extend(["", "## Selected info-field presence and common values", ""])
    for path, distribution in summary["info_field_distributions"].items():
        top_values = ", ".join(
            f"{json.dumps(item['value'], ensure_ascii=False)} ({item['count']})"
            for item in distribution["top_values"][:8]
        ) or "no non-null values"
        lines.append(
            f"- {path}: present in {distribution['present_in_releases']}/"
            f"{summary['results_count']} releases ({distribution['present_percent']}%); "
            f"top values: {top_values}"
        )
    lines.extend([
        "",
        "## Video-stream field coverage",
        "",
        "| Field path | Releases containing field | Types | Samples |",
        "|---|---:|---|---|",
    ])
    video_paths = [
        (path, field) for path, field in schema["fields"].items()
        if path.startswith("ffprobe[].") or path.startswith("ffprobe.streams[].")
    ]
    video_paths.sort(key=lambda item: (-item[1]["release_count"], item[0]))
    if not video_paths:
        lines.append("| (no video-stream fields detected) | 0 | — | — |")
    for path, field in video_paths[:50]:
        samples = ", ".join(json.dumps(x, ensure_ascii=False) for x in field["samples"][:3])
        samples = samples.replace("|", "\\|")
        lines.append(
            f"| {path} | {field['release_count']}/{summary['results_count']} | "
            f"{', '.join(field['types'])} | {samples} |"
        )
    lines.extend([
        "",
        "## Redacted fields",
        "",
        "Magnet URLs, Details/URL fields, and credential-like values were removed before saving.",
    ])
    if summary["redacted_field_occurrences"]:
        for path, count in sorted(summary["redacted_field_occurrences"].items()):
            lines.append(f"- {path}: redacted in {count} occurrence(s)")
    else:
        lines.append("- No matching fields needed redaction.")
    lines.extend([
        "",
        "## Interpretation note",
        "",
        "Title markers are string evidence only, not proof of the encoded video's HDR format. "
        "Compare the sanitized release records with JacRed/ffprobe metadata before using a "
        "marker for HDR classification.",
        "",
    ])
    return "\n".join(lines)


def write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Collect a sanitized Lanterns (2026) result set from local JacRed for HDR field analysis."
    )
    parser.add_argument("--url", dest="source_url", default=DEFAULT_SOURCE_URL,
                        help="Loopback JacRed JSON endpoint; defaults to local 127.0.0.1:9117.")
    parser.add_argument("--query", default=DEFAULT_QUERY, help=f"Search title (default: {DEFAULT_QUERY!r}).")
    parser.add_argument("--year", default=DEFAULT_YEAR, help=f"Year filter (default: {DEFAULT_YEAR}).")
    parser.add_argument("--category", default=DEFAULT_CATEGORY, help=f"JacRed category filter (default: {DEFAULT_CATEGORY}).")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="Result cap 1-1000 (default: 1000).")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help=f"HTTP timeout in seconds (default: {DEFAULT_TIMEOUT}).")
    parser.add_argument("--output-dir", type=Path, default=None,
                        help="Output directory; default is a timestamped directory under /tmp.")
    args = parser.parse_args(argv)
    if not is_loopback_url(args.source_url):
        parser.error("refusing non-loopback JacRed URL; this collector is intentionally limited to local JacRed")
    if not args.query.strip():
        parser.error("--query must not be empty")
    if not args.category.strip():
        parser.error("--category must not be empty")
    if not 1 <= args.limit <= 1000:
        parser.error("--limit must be between 1 and 1000")
    if not 1 <= args.timeout <= 300:
        parser.error("--timeout must be between 1 and 300 seconds")
    return args


def fetch_once(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    """Perform exactly one bounded HTTP request, without automatic retries."""
    params = {
        "q": args.query.strip(),
        "category": args.category.strip(),
        "limit": str(args.limit),
    }
    if args.year.strip():
        params["year"] = args.year.strip()
    parsed = urllib.parse.urlsplit(args.source_url)
    existing = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    query = urllib.parse.urlencode(existing + list(params.items()))
    url = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, query, ""))
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "JacRed-HDR-Sample-Collector/1.0",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=args.timeout) as response:
            status = response.status
            body = response.read()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(
            f"Local JacRed request failed ({type(exc).__name__}: {exc}). "
            "No automatic retry was attempted."
        ) from exc
    if status != 200:
        raise RuntimeError(f"Local JacRed returned HTTP {status}; no retry was attempted.")
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Local JacRed returned invalid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("Expected a JSON object from JacRed.")
    results = payload.get("Results") or []
    if not isinstance(results, list):
        raise RuntimeError("JacRed JSON field Results is not a list.")
    payload["Results"] = [item for item in results if isinstance(item, dict)]
    return payload, len(body)


def collect(args: argparse.Namespace) -> tuple[Path, Path, dict[str, Any]]:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output_dir = args.output_dir or Path("/tmp") / f"jacred-hdr-lanterns-2026-{timestamp}"
    output_dir = output_dir.expanduser().resolve()
    if output_dir.exists() and any(output_dir.iterdir()):
        raise RuntimeError(f"Output directory is not empty: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)

    payload, response_bytes = fetch_once(args)
    source_records = payload["Results"]
    redactions: dict[str, dict[str, Any]] = {}
    sanitized_records = [
        make_sanitized_release(item, index, redactions)
        for index, item in enumerate(source_records, start=1)
    ]
    schema = build_field_schema(source_records)
    observations = [make_hdr_observation(record) for record in sanitized_records]
    summary = build_summary(
        args, payload, response_bytes, source_records, sanitized_records, schema, observations
    )
    schema["redacted_fields"] = {
        path: {"occurrences": data["occurrences"], "types": sorted(data["types"])}
        for path, data in sorted(redactions.items())
    }

    (output_dir / "sanitized_releases.jsonl").write_text(
        "".join(
            json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
            for record in sanitized_records
        ),
        encoding="utf-8",
    )
    write_json(output_dir / "field_schema.json", schema)
    write_json(output_dir / "hdr_observations.json", observations)
    write_json(output_dir / "summary.json", summary)
    (output_dir / "summary.md").write_text(markdown_summary(summary, schema), encoding="utf-8")
    (output_dir / "README.txt").write_text(
        "Sanitized HDR metadata sample from the local JacRed instance.\n"
        "No torrent content was downloaded. Magnet URLs and Details/URL/credential-like\n"
        "fields were removed; an infohash may remain to identify duplicates. Title markers\n"
        "are candidates for analysis, not proof of the encoded video HDR format.\n\n"
        f"Query: {args.query}\nYear: {args.year}\nCategory: {args.category}\n"
        f"Results: {len(sanitized_records)}\n",
        encoding="utf-8",
    )
    archive_path = output_dir.with_suffix(".zip")
    if archive_path.exists():
        raise RuntimeError(f"Archive already exists: {archive_path}")
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(output_dir.iterdir()):
            archive.write(path, arcname=f"{output_dir.name}/{path.name}")
    return output_dir, archive_path, summary


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        output_dir, archive_path, summary = collect(args)
    except (RuntimeError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print("HDR sample collection completed.")
    print(f"Query: {args.query!r}; year={args.year}; category={args.category}; limit={args.limit}")
    print(f"HTTP status: {summary['source']['http_status']}")
    print(f"Results: {summary['results_count']}")
    print(f"Unique infohashes: {summary['unique_infohash_count']}")
    print(f"Releases with video streams: {summary['video_streams_found']}")
    print(f"Total video streams: {summary['video_stream_count_total']}")
    print(f"Field paths profiled: {summary['field_schema_path_count']}")
    print("HDR marker observations:")
    if summary["title_and_info_marker_counts"]:
        for marker, count in summary["title_and_info_marker_counts"].items():
            print(f"  {marker}: {count}")
    else:
        print("  No recognized markers in this result set.")
    print(f"Summary: {output_dir / 'summary.md'}")
    print(f"Dataset: {output_dir / 'sanitized_releases.jsonl'}")
    print(f"Archive: {archive_path}")
    if summary["results_count"] == 0:
        print("WARNING: no matching releases; a broader local-only query may be needed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
