# ExtremeCloud IQ BSSID export

Command-line tool that lists every access point in an ExtremeCloud IQ organization and writes a CSV row for each interface you choose. Rows are sorted by site, building, floor, and AP name.

The shipped `config.json` asks for **wifi1.0**. That is the radio's base BSSID: the address whose last hex digit is `0`. `wifi1.1` is 4 higher and carries the first SSID. Each later BSS is one higher than that.

An assistant setting this up on a new machine should follow [AI.md](AI.md).

## Requirements

- Python 3.10 or newer
- An ExtremeCloud IQ bearer token

## Setup

```bash
python3 -m pip install -r requirements.txt
cp .env.example .env
```

Put the token in `.env` as `XIQ_API_TOKEN`. The script reads that file itself and never prints the token. `.env` is gitignored.

## Choose the radio and BSS

Edit `interfaces` in `config.json`. Each entry is a radio name, a dot, and a BSS index.

```json
{
  "interfaces": ["wifi1.0"],
  "ssids": [],
  "sites": [],
  "connected_only": false,
  "base_url": "https://api.extremecloudiq.com",
  "out": ""
}
```

| Field | Meaning |
|---|---|
| `interfaces` | Interfaces to export. Default: `["wifi1.0"]`. Add `"wifi1.1"` or `"wifi0.1"` to export those too. `wifiN.0` through `wifiN.12` are valid. |
| `ssids` | Optional. Keep a row only when its SSID is in this list. Matching is case-sensitive. `wifiN.0` has no SSID, so a filter drops it. |
| `sites` | Optional. Keep these sites. Matching is case-insensitive. |
| `connected_only` | `true` skips disconnected access points. Their radio data may be stale or empty. |
| `base_url` | API host. Default: `https://api.extremecloudiq.com`. |
| `out` | CSV path. Leave `""` to write `xiq_bssids_YYYYMMDD_HHMM.csv` in the current directory. |

On the access point, `show interface` names the base address `Wifi1` and the first SSID `Wifi1.1`. This export calls that base address `wifi1.0`, because the base BSSID is the address ending in `0`.

## Authentication

The script sends `XIQ_API_TOKEN` as `Authorization: Bearer <token>`. If the variable is already set in the environment, that value wins over `.env`. If the token is missing and both `XIQ_USERNAME` and `XIQ_PASSWORD` are set, the script calls `POST /login` and uses `access_token` from the response. A token that is already set is used as-is.

## Usage

Run it from the project directory:

```bash
python3 xiq_bssid_export.py
```

Flags override `config.json` for one run:

```bash
python3 xiq_bssid_export.py --interface wifi1.1 --interface wifi1.2 --out wifi1-ssids.csv
python3 xiq_bssid_export.py --site "Boston" --connected-only --verbose
python3 xiq_bssid_export.py --ssid "Corp"
```

| Flag | Meaning |
|---|---|
| `--config PATH` | Settings file. Default: `config.json`. |
| `--interface NAME` | Repeatable. Replaces the `interfaces` list. Example: `wifi1.0`. |
| `--ssid NAME` | Repeatable. Replaces the `ssids` list. Case-sensitive. |
| `--site NAME` | Repeatable. Replaces the `sites` list. Case-insensitive. |
| `--connected-only` / `--no-connected-only` | Overrides `connected_only`. |
| `--out PATH` | Overrides `out`. |
| `--base-url URL` | Overrides `base_url`. |
| `--verbose` | Log each request URL and the page counts. The token is not included. |

## How the BSSID is chosen

For each access point whose `device_function` is `AP`:

1. The location tree and the device breadcrumb supply site, building, and floor.
2. `GET /devices/radio-information` supplies the radios. Device IDs are sent in batches of 50. This endpoint rejects a page size above 50, so the script asks for 50 records per page.
3. Each requested interface is matched to the radio of the same name (`wifi1.0` and `wifi1.1` both use the `wifi1` radio).
4. IQ Engine gives that radio a 16-address block aligned to a trailing `0`. That address is `wifiN.0`. `wifiN.1` is 4 higher. `wifiN.2` is 5 higher. The API `mac_address` is `wifiN.0` when no SSID is up, and `wifiN.1` when one is. The script moves an address ending in `4` back to `0` before it applies the BSS index.
5. `Radio MAC` is always `wifiN.0`. `BSSID` is the requested BSS. When a WLAN in the API uses that BSSID, its SSID, status, and network policy are written on the row.
6. If the access point has no matching radio, one row is written per requested interface with the note `no wifi1 radio reported`.

`wifi1.0` on AP-EXAMPLE-01 is `aa:bb:cc:00:00:60`. The API returns `aa:bb:cc:00:00:64`, and `show interface` labels that address `Wifi1.1`.

## CSV columns

Site, Building, Floor, AP Name, Serial, Model, Connected, Radio, Radio MAC, WLAN Index, Inferred Subinterface, SSID, BSSID, SSID Status, Network Policy, Location Source, Notes.

`Inferred Subinterface` is the interface from `config.json`, such as `wifi1.0`. `WLAN Index` is the number after the dot. `Notes` records an API address that was not already the base, for example `API radio MAC aa:bb:cc:00:00:64 is wifi1.1`. `Location Source` is `tree` when the location tree identified the place, `breadcrumb-guess` when a breadcrumb id was missing from the tree, and `unassigned` when the access point has no location. Unassigned access points use the site name `(unassigned)`.

MAC addresses are written as lowercase colon-separated values.

## Run summary

When the file is written, the script prints the output path, access point count, how many access points reported a selected radio, rows written, access points with no location, unrecognized location types, and the interfaces that were exported.

## Errors and retries

Each request times out after 30 seconds. HTTP 429 and 5xx responses are retried after 1, 2, 4, 8, and 16 seconds. A `Retry-After` header replaces that delay. HTTP 401 exits with `token invalid or expired` and the endpoint. HTTP 403 exits with `token lacks permission` and the endpoint.

## Verify on an access point

Checked against `show interface` on AP-EXAMPLE-01, serial `XXXXXXXXXXXXXX`:

| Interface | MAC | SSID |
|---|---|---|
| wifi1.0 | `aa:bb:cc:00:00:60` | none |
| wifi1.1 | `aa:bb:cc:00:00:64` | EXAMPLE-SSID-1 |
| wifi1.2 | `aa:bb:cc:00:00:65` | EXAMPLE-SSID-2 |

## What stays out of git

`.gitignore` excludes `.env`, API token responses, generated CSV files, `LOCAL_HANDOFF.md`, and Python bytecode. Do not commit tokens, passwords, or exported inventories.
