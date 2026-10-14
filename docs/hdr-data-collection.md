# HDR metadata collection / Сбор данных для анализа HDR

## Purpose

The collector gathers actual release metadata from the **local JacRed instance** to find out which HDR-related fields are present before changing the adapter's HDR classification logic.

It defaults to the query **Lanterns**, year **2026**, category **tv_**, and the local endpoint **http://127.0.0.1:9117/api/v2.0/indexers/all/results**. It performs exactly one bounded HTTP request and never retries automatically. The script refuses non-loopback URLs, so it cannot accidentally probe public JacRed.

No torrent content is downloaded. Magnet URLs, Details/URL fields, and credential-like fields are removed from the saved data. The BTIH infohash may be retained for deduplication and comparison. URLs embedded in string values are replaced with **[REDACTED_URL]**.

## Run on MINI-Ubuntu

Download the collector from the working branch:

~~~bash
curl --fail --location \
  --connect-timeout 10 --max-time 60 \
  -o /tmp/collect_hdr_samples.py \
  https://raw.githubusercontent.com/evgendman/jacred2prowlarr/tool/hdr-sample-collector/tools/collect_hdr_samples.py

/usr/bin/python3 /tmp/collect_hdr_samples.py
~~~

The default query is equivalent to:

- query: Lanterns
- year: 2026
- category: tv_
- maximum results: 1000
- request timeout: 45 seconds

The result folder and ZIP archive are created under /tmp with a UTC timestamp in their names.

Optional parameters, for example to repeat the search without a year filter if the result is empty:

~~~bash
/usr/bin/python3 /tmp/collect_hdr_samples.py \
  --query Lanterns --year '' --category tv_ --limit 1000 \
  --output-dir /tmp/jacred-hdr-lanterns-unfiltered
~~~

Choose a new output directory for each run; a non-empty output directory is not overwritten.

## Generated files

- **summary.md** — quick counts, field coverage, common values and HDR-like text markers.
- **summary.json** — machine-readable run summary and distributions for selected info fields.
- **field_schema.json** — field paths, JSON types, per-release presence percentage and a few sanitized examples.
- **hdr_observations.json** — one compact record per release: title markers, selected info fields and selected video-stream fields.
- **sanitized_releases.jsonl** — sanitized release records preserving all non-redacted metadata, including the full available ffprobe stream objects.
- **README.txt** — dataset notes.
- **jacred-hdr-lanterns-2026-<timestamp>.zip** — archive containing the files above.

The collector profiles fields including info.videotype, info.video_type, info.quality, and nested ffprobe attributes such as codec_name, profile, pix_fmt, color_transfer, color_primaries, bit depth, and side_data_list whenever JacRed returns them.

## Interpretation and safety

A title marker such as HDR10+ or Dolby Vision is only evidence of what the release name claims. It does not independently prove the actual encoded video format. Compare the title labels with structured JacRed/ffprobe fields before implementing classification rules. A missing field is recorded as missing; the collector does not infer values.

The script is a diagnostic utility and does not modify the installed adapter, systemd configuration, or JacRed database. It uses only the local API.

---

## По-русски

### Назначение

Сборщик получает реальные метаданные раздач из **локального экземпляра JacRed**, чтобы выяснить, какие поля, связанные с HDR, фактически присутствуют в ответах. По этим данным можно будет дорабатывать классификацию HDR в адаптере, не делая предположений о структуре API.

По умолчанию используется поиск **Lanterns**, год **2026**, категория **tv_** и локальный API **http://127.0.0.1:9117/api/v2.0/indexers/all/results**. Выполняется ровно один ограниченный по времени HTTP-запрос, автоматических повторов нет. Скрипт отказывается обращаться к URL не на loopback, поэтому не может случайно опрашивать публичный JacRed.

Содержимое торрентов не скачивается. Из сохраняемых данных удаляются magnet-ссылки, поля URL/Details и поля, похожие на секреты. BTIH infohash может сохраняться для выявления дублей и сопоставления записей. URL, встроенные в текстовые значения, заменяются на **[REDACTED_URL]**.

### Запуск на MINI-Ubuntu

Команда загрузки и запуска приведена выше. Она сохраняет скрипт в /tmp/collect_hdr_samples.py и затем запускает его системным Python.

Результаты появятся в отдельном каталоге и ZIP-архиве в /tmp. Основные файлы:

- **summary.md** — краткий отчёт о количестве результатов, наличии полей и найденных HDR-маркерах;
- **summary.json** — машинно-читаемая сводка;
- **field_schema.json** — пути полей, типы, частота наличия и безопасные примеры значений;
- **hdr_observations.json** — маркеры в названиях, выбранные поля info и видеопотоков;
- **sanitized_releases.jsonl** — метаданные всех найденных релизов после удаления URL и чувствительных полей.

Если поиск не найдёт релизов, можно выполнить один отдельный сбор без фильтра по году, указав --year '' и новый каталог вывода. Каждый запуск — один поисковый запрос, без автоматических повторов.

### Как интерпретировать результаты

Маркеры HDR10+ и Dolby Vision в названии показывают лишь то, что заявлено в имени релиза. Они не являются самостоятельным доказательством формата закодированного видео. Перед изменением алгоритма нужно сопоставить название с реальными полями JacRed/ffprobe. Отсутствующие поля должны остаться отсутствующими — сборщик ничего не додумывает.

Скрипт предназначен только для диагностики. Он не меняет установленный адаптер, systemd и базу JacRed.
