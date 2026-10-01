# AI setup

You are setting up the ExtremeCloud IQ BSSID export. Do the work in the project directory. Do not print the API token, a password, or any `Authorization` header.

## What this tool does

`xiq_bssid_export.py` writes one CSV row per access point for each interface in `config.json`. The default interface is `wifi1.0`.

`wifi1.0` is the base BSSID of radio `wifi1`. Its last hex digit is `0`. `wifi1.1` is 4 higher and is the first SSID. `wifi1.2` is one higher than `wifi1.1`. The API field `mac_address` is often `wifi1.1` when an SSID is up. Do not copy that field into the CSV as `wifi1.0`.

`Radio MAC` in the CSV is always the base (`wifiN.0`). `BSSID` is the interface the customer asked for.

## Setup

1. Require Python 3.10 or newer.
2. Install dependencies: `python -m pip install -r requirements.txt`
3. If `.env` does not exist, copy `.env.example` to `.env`.
4. Set `XIQ_API_TOKEN` in `.env` to the customer's ExtremeCloud IQ bearer token. Leave `XIQ_USERNAME` and `XIQ_PASSWORD` commented unless the customer has no token. The script reads `.env` itself. An existing environment variable wins.
5. Leave `config.json` at `["wifi1.0"]` unless the customer names other interfaces.

## Choosing interfaces

Edit only the keys already in `config.json`. Unknown keys make the script exit.

- One base radio: `"interfaces": ["wifi1.0"]`
- First SSID on that radio: `"interfaces": ["wifi1.1"]`
- Several at once: `"interfaces": ["wifi1.0", "wifi1.1", "wifi0.1"]`
- Valid indexes are `wifiN.0` through `wifiN.12`.
- `ssids` filters the SSID column. It removes `wifiN.0` rows, because that interface has no SSID.
- `sites` is case-insensitive. `ssids` is case-sensitive.
- `out` as `""` writes `xiq_bssids_YYYYMMDD_HHMM.csv`.

A one-run override does not edit the file:

```bash
python xiq_bssid_export.py --interface wifi1.1 --out wifi1.1.csv
```

## Run

From the project directory:

```bash
python xiq_bssid_export.py
```

Expect a stderr summary: output path, AP count, rows written, and a line starting with `Exporting`. HTTP 401 means the token is missing or expired. Do not echo the token while fixing that.

## Check the result

Open the CSV. For a `wifi1.0` row, `BSSID` equals `Radio MAC` and ends in hex digit `0`. `SSID` is blank. For `wifi1.1`, `BSSID` is the radio MAC plus 4, and `SSID` is filled when the API reported that WLAN.

A known-good access point, AP-EXAMPLE-01 serial `XXXXXXXXXXXXXX`, resolves as:

- `wifi1.0` = `aa:bb:cc:00:00:60`
- `wifi1.1` = `aa:bb:cc:00:00:64` with SSID `EXAMPLE-SSID-1`
- `wifi1.2` = `aa:bb:cc:00:00:65` with SSID `EXAMPLE-SSID-2`

`Notes` may say `API radio MAC aa:bb:cc:00:00:64 is wifi1.1` on the `wifi1.0` row. That note is expected.

## Do not publish

Do not commit `.env`, `response_*.json`, `*.csv`, or `LOCAL_HANDOFF.md`. Do not put the token in `config.json`, the README, or a commit message.
