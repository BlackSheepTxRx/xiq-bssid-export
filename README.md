# ExtremeCloud IQ wifi1.0 BSSID export

Private command-line tool that lists every access point in an ExtremeCloud IQ organization and writes one CSV row for the **wifi1.0** BSSID. Rows are sorted by site, building, floor, and AP name.

The API does not return subinterface names. On these access points, wifi1.0 is the BSSID that equals the wifi1 radio MAC. wifi1.1, wifi1.2, and later SSIDs use the following MAC addresses and are left out of the file.

## Requirements

- Python 3.10 or newer
- An ExtremeCloud IQ bearer token

## Setup

```bash
python3 -m pip install -r requirements.txt
cp .env.example .env
```

Put the token in `.env` or export it in the shell. `.env` is gitignored.

## Authentication

The script reads `XIQ_API_TOKEN` and sends it as `Authorization: Bearer <token>`. It never prints or logs the token. If the variable is missing, the script exits and names the variable.

Optional fallback: when `XIQ_API_TOKEN` is unset and both `XIQ_USERNAME` and `XIQ_PASSWORD` are set, the script calls `POST /login` and uses `access_token` from the response. A token that is already set is used as-is.

```bash
export XIQ_API_TOKEN="paste-token-here"
python3 xiq_wifi1_bssid_export.py
```

To load a local `.env` file without printing it:

```bash
set -a
source .env
set +a
python3 xiq_wifi1_bssid_export.py
```

## Usage

```bash
python3 xiq_wifi1_bssid_export.py
python3 xiq_wifi1_bssid_export.py --site "Boston" --connected-only --out boston.csv
python3 xiq_wifi1_bssid_export.py --ssid "Corp" --verbose
```

The default output file is `xiq_wifi1_bssids_YYYYMMDD_HHMM.csv` in the current directory.

### Flags

| Flag | Meaning |
|---|---|
| `--radio NAME` | Radio to match, case-insensitively. Default: `wifi1`. |
| `--ssid NAME` | Repeatable. Keep the wifi1.0 row only when its SSID is one of these names. Matching is case-sensitive. |
| `--site NAME` | Repeatable. Keep these sites. Matching is case-insensitive. |
| `--connected-only` | Skip disconnected access points. Their radio data may be stale or empty. |
| `--out PATH` | CSV path. |
| `--base-url URL` | API base URL. Default: `https://api.extremecloudiq.com`. |
| `--verbose` | Log each request URL and the page counts. The token is not included. |

## How wifi1.0 is chosen

For each access point whose `device_function` is `AP`:

1. The location tree and the device breadcrumb supply site, building, and floor.
2. `GET /devices/radio-information` supplies the radios. Device IDs are sent in batches of 50. This endpoint rejects a page size above 50, so the script asks for 50 records per page.
3. The radio whose name matches `--radio` is kept.
4. The WLAN whose BSSID equals that radio's MAC is wifi1.0. Its SSID, status, and network policy are written on the row.
5. If the radio is present but no SSID uses that MAC, the row still uses the radio MAC as the wifi1.0 BSSID and leaves the SSID blank.
6. If the access point has no matching radio, one row is written with the note `no wifi1 radio reported`.

## CSV columns

Site, Building, Floor, AP Name, Serial, Model, Connected, Radio, Radio MAC, WLAN Index, Inferred Subinterface, SSID, BSSID, SSID Status, Network Policy, Location Source, Notes.

`WLAN Index` is `0` and `Inferred Subinterface` is `wifi1.0` when the radio was found. `Location Source` is `tree` when the location tree identified the place, `breadcrumb-guess` when a breadcrumb id was missing from the tree, and `unassigned` when the access point has no location. Unassigned access points use the site name `(unassigned)`.

MAC addresses are written as lowercase colon-separated values.

## Run summary

When the file is written, the script prints:

- output path
- access point count
- access points that reported the selected radio
- rows written
- access points with no location
- unrecognized location types
- a one-line warning that wifi1.0 is inferred from the radio MAC

## Errors and retries

Each request times out after 30 seconds. HTTP 429 and 5xx responses are retried after 1, 2, 4, 8, and 16 seconds. A `Retry-After` header replaces that delay. HTTP 401 exits with `token invalid or expired` and the endpoint. HTTP 403 exits with `token lacks permission` and the endpoint.

## Verify on an access point

Before trusting the `wifi1.0` label, SSH to one access point and run `show interface`. The wifi1.0 MAC should match the BSSID in the CSV for that AP.

## What stays out of git

`.gitignore` excludes `.env`, API token responses, generated CSV files, and Python bytecode. Do not commit tokens, passwords, or exported inventories.
