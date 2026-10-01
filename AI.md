# AI setup

You are setting up the ExtremeCloud IQ BSSID export. Do the work in the project directory. Do not print the API token, a password, or any `Authorization` header.

## What this tool does

`xiq_bssid_export.py` writes one CSV row per access point for each interface in `config.json`. The default interface is `wifi1.0`.

`wifi1.0` is the base BSSID of radio `wifi1`. Its last hex digit is `0`. `wifi1.1` is 4 higher and is the first SSID. `wifi1.2` is one higher than `wifi1.1`. The API field `mac_address` is often `wifi1.1` when an SSID is up. Do not copy that field into the CSV as `wifi1.0`.

`Radio MAC` in the CSV is the base (`wifiN.0`) when that base is known. For `wifi1.0`, `BSSID` is the base and `SSID` is blank (`Status` `base`). For `wifi1.1` and later, fill `BSSID` only when that calculated address appears in the radio's `wlans[]`. If it does not, leave `BSSID` blank, keep the row, and set `Status` to `not-reported`.

## Setup

1. Require Python 3.10 or newer.
2. Install dependencies: `python -m pip install -r requirements.txt`
3. If `.env` does not exist, copy `.env.example` to `.env`.
4. Set `XIQ_API_TOKEN` in `.env` to the customer's ExtremeCloud IQ bearer token. Leave `XIQ_USERNAME` and `XIQ_PASSWORD` commented unless the customer has no token. The script reads `.env` from the working directory, then from the directory that contains `xiq_bssid_export.py`. An existing environment variable wins. The default `config.json` is found the same way.
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

Expect a stderr summary: output path, AP count, rows written, `APs with WLAN BSSIDs outside the expected pattern: N`, and a line starting with `Exporting`. `--verbose` lists the hostname and model of each mismatched access point. HTTP 401 means the token is missing or expired. Do not echo the token while fixing that.

## Check the result

Open the CSV. For a `wifi1.0` row, `Status` is `base`, `BSSID` equals `Radio MAC`, and the address ends in hex digit `0`. `SSID` is blank. For `wifi1.1`, `Status` is `confirmed` only when the API listed that WLAN, and `BSSID` is the base plus 4. A requested BSS with no matching WLAN has a blank `BSSID` and `Status` `not-reported`.

Use placeholders in any example you write. A fictional access point `AP-EXAMPLE-01`, serial `XXXXXXXXXXXXXX`, with base `aa:bb:cc:00:00:60` resolves as:

- `wifi1.0` = `aa:bb:cc:00:00:60`, Status `base`
- `wifi1.1` = `aa:bb:cc:00:00:64` with SSID `EXAMPLE-SSID-1`, Status `confirmed`
- `wifi1.2` = `aa:bb:cc:00:00:65` with SSID `EXAMPLE-SSID-2`, Status `confirmed`

`Status` is `no-radio` when the access point did not report that radio, and `unexpected-mac` when the API radio MAC ends in hex digit 1, 2, or 3.

## Do not publish

Do not commit `.env`, `response_*.json`, `*.csv`, or `LOCAL_HANDOFF.md`. Do not put the token in `config.json`, the README, or a commit message. Real customer hostnames, serials, MAC addresses, and SSIDs must never be committed. Use placeholders such as `AP-EXAMPLE-01`, `XXXXXXXXXXXXXX`, `aa:bb:cc:00:00:60`, and `EXAMPLE-SSID-1`.
