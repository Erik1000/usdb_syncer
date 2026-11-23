---
name: usdb-syncer-api
description: Search a USDB Syncer song collection, inspect download status, stream audio, and request downloads or forced re-downloads through its REST API. Use when automating a running USDB Syncer server.
---

# USDB Syncer REST API

Use the server's `/api` endpoints for automation. The `/fragments` routes return HTML for the browser and are not the REST API.

## Connect and check capabilities

Use the host and port of the running server. To start an instance from an installed package:

```sh
usdb_syncer serve --host 127.0.0.1 --port 8080 --show-nonlocal --allow-downloading
```

From a source checkout, prefix that command with `uv run`. The desktop webserver dialog also supports a persisted Host value and automatic startup.

- `--host` accepts an IP address or hostname. Omit it or leave the dialog field empty to use the automatically detected LAN address.
- `127.0.0.1` limits access to the server computer. `0.0.0.0` listens on all IPv4 interfaces; clients use the server's actual IP address. IPv6 URLs need brackets, for example `http://[::1]:8080`.
- Omit `--port` or use `0` to let the OS assign a port; read the resulting URL from the log or desktop dialog.
- `--show-nonlocal` includes cached USDB songs outside the local collection in search results. This does not refresh the USDB catalog. Initialize the collection, credentials, song folder, and download settings in USDB Syncer before automation.
- `--allow-downloading` permits downloads, including forced re-downloads. Downloads use the server's configured folder and resource options.
- The server has no authentication. Use a trusted interface/network or an authenticated reverse proxy when exposing it remotely.

Start by requesting `GET /api`:

```json
{"api_version":1,"show_nonlocal_songs":true,"allow_downloading":true}
```

## Endpoints

All endpoints return JSON except successful audio responses. Paths below are relative to the server origin.

| Method | Path | Result |
| --- | --- | --- |
| GET | `/api` | API version and enabled capabilities |
| GET | `/api/songs` | Array of matching songs; defaults to 100 results |
| GET | `/api/songs/{song_id}` | One song's metadata and current status |
| GET | `/api/songs/{song_id}/audio` | Local audio file, with MIME type inferred from its extension |
| GET | `/api/songs/{song_id}/download` | Current download status; works when downloading is disabled |
| POST | `/api/songs/{song_id}/download` | Queue a download; returns HTTP 202 with status and a `Location` header for polling |

Song objects contain `song_id`, `artist`, `title`, `genre`, `year` (possibly null), `language`, `creator`, `edition`, `golden_notes`, `rating`, `views`, `sample_url`, `usdb_mtime`, `tags`, `status`, `status_code`, `is_downloaded`, `is_local`, and `pinned`. Internal filesystem paths are excluded. Use the numeric `song_id` returned by search for subsequent requests.

Display a song as downloaded only when `is_downloaded` is true. This requires both the lyrics/song TXT file and the audio file to exist on disk. `is_local` follows the same rule in API song objects. Sync metadata, a song folder, lyrics alone, or audio alone are insufficient. Optional video and image files are not required. Availability is checked on every response, so deleted files stop counting as downloaded.

Download status objects contain `song_id`, `status`, `status_code`, and `is_downloaded`. The status codes describe synchronization and queue state; `Synchronized` alone does not establish that both files are currently available:

| Code | Status string | Meaning |
| --- | --- | --- |
| 0 | `""` | No local download status |
| 1 | `Synchronized` | Local song matches known USDB metadata |
| 2 | `Outdated` | Local song needs synchronization |
| 3 | `Pending` | Queued |
| 4 | `Downloading` | Running |
| 5 | `Failed` | Download failed |

## Search and pagination

URL-encode query values. Text and enum parameters are case sensitive.

| Parameter | Values |
| --- | --- |
| `search` | Free text |
| `limit` | Integer 1–1000; default 100 |
| `offset` | Integer 0–2147483647; default 0 |
| `sort_by` | `artist` (default), `title`, `year`, `likes` |
| `sort_order` | `asc` (default), `desc` |
| `artist`, `title`, `edition`, `language`, `genre`, `creator` | Exact filter values; repeat the parameter for multiple alternatives |
| `rating` | Numeric rating; repeatable |
| `status` | Numeric status code from the table; repeatable |
| `year` | Integer year; repeatable |
| `views` | Inclusive range such as `10-100` or open range `100-`; repeatable |
| `golden_notes` | `true` or `false` |

Use repeated keys, such as `artist=Queen&artist=ABBA`, rather than comma-separated lists. When nonlocal results are disabled, search restricts status filters to Synchronized and Outdated and excludes songs missing lyrics or audio before pagination. Direct song/status/audio lookup uses a known ID regardless of the search visibility setting.

Advance `offset` by the number of results returned. Stop on an empty page or a page shorter than `limit`. Pagination has no snapshot guarantee while the collection changes.

```sh
curl --get 'http://127.0.0.1:8080/api/songs' --data-urlencode 'search=Queen' --data 'limit=20' --data 'offset=0'
curl 'http://127.0.0.1:8080/api/songs/123/download'
```

In Windows PowerShell, use `curl.exe` to avoid the legacy `curl` alias, or use `Invoke-RestMethod` as shown below.

## Download and force re-download

An empty POST body or `{}` requests a normal synchronization. Existing unchanged resources may be reused. To download resources again even when their source is unchanged, send exactly a JSON boolean:

```http
POST /api/songs/123/download
Content-Type: application/json

{"force_redownload":true}
```

```powershell
$origin = 'http://127.0.0.1:8080'
Invoke-RestMethod -Method Post -Uri "$origin/api/songs/123/download" -ContentType 'application/json' -Body '{"force_redownload":true}'
Invoke-RestMethod -Uri "$origin/api/songs/123/download"
```

Forced downloads still honor configured resource options and pinned songs. An already queued/running song returns 409 even when force is true. A 202 response confirms queue acceptance, not completion.

Poll the returned `Location` path or GET the download endpoint every few seconds while `status_code` is 3 or 4. Stop on a terminal status or a reasonable deadline, and report the final status and `is_downloaded` value. If `is_downloaded` is false, report the song as incomplete even when `status_code` is 1. `Failed` requires inspecting the server logs; do not repeatedly force re-download automatically. After a POST timeout or lost connection, inspect status before considering another POST.

## Errors

Errors use this shape, including missing API routes and unsupported HTTP methods:

```json
{"error":{"code":409,"message":"Song is already queued or downloading"}}
```

- 400: malformed JSON, invalid filters/pagination, unknown body fields, or a non-boolean `force_redownload`.
- 403: downloading disabled; read-only endpoints remain available.
- 404: unknown song, missing local audio, or unknown route.
- 405: unsupported method; inspect the `Allow` header.
- 409: song pinned or already queued/downloading. Do not retry without a relevant state change.
- 415: nonempty download request body without a JSON media type.
- 500: server failure; inspect the server logs before retrying.
