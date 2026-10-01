#!/usr/bin/env python3
"""Export ExtremeCloud IQ AP BSSIDs for one radio, sorted by site and floor.

The script reads every access point in an ExtremeCloud IQ organization, keeps
the selected radio (wifi1 by default), and writes one CSV row for wifi1.0.
That row is the BSSID equal to the radio MAC. Later subinterfaces (wifi1.1,
wifi1.2, and so on) are omitted. Rows are sorted by site, building, floor,
and AP name.

Setup
-----
Python 3.10 or newer, plus the ``requests`` package::

    python3 -m pip install -r requirements.txt

Authentication
--------------
Set a bearer token in the environment. The script sends it as
``Authorization: Bearer <token>`` and never prints or logs the token::

    export XIQ_API_TOKEN="paste-token-here"
    python3 xiq_wifi1_bssid_export.py

If ``XIQ_API_TOKEN`` is missing, the script exits with a message that names
the variable. Optional fallback: when the token is unset and both
``XIQ_USERNAME`` and ``XIQ_PASSWORD`` are set, the script calls ``POST /login``
and uses ``access_token`` from the response. A token that is already set is
used as-is; the username and password are not sent.

Flags
-----
``--radio NAME``
    Radio name to match, case-insensitively. Default: ``wifi1``.
``--ssid NAME``
    Repeatable. Keep the wifi1.0 row only when its SSID is one of these
    names (case-sensitive).
``--site NAME``
    Repeatable. Keep only these sites. Matching is case-insensitive.
``--connected-only``
    Skip disconnected APs. Their radio data may be stale or empty.
``--out PATH``
    CSV path. Default: ``xiq_wifi1_bssids_YYYYMMDD_HHMM.csv`` in the
    current directory.
``--base-url URL``
    API base URL. Default: ``https://api.extremecloudiq.com``.
``--verbose``
    Log each request URL (the token is not included) and the page counts.

The CSV columns are Site, Building, Floor, AP Name, Serial, Model, Connected,
Radio, Radio MAC, WLAN Index, Inferred Subinterface, SSID, BSSID, SSID Status,
Network Policy, Location Source, and Notes.

``Inferred Subinterface`` is ``wifi1.0`` on a normal run. The API does not
return subinterface names. wifi1.0 is inferred as the BSSID that equals the
wifi1 radio MAC. Each later SSID on that radio uses the next MAC address
(wifi1.1, wifi1.2, and so on) and is left out of the CSV. The script prints
a one-line warning about that at the end of every run.

Verification
------------
Before trusting the Inferred Subinterface column, pick one AP, SSH to it, and
run ``show interface``. The wifi1.0 MAC should match the BSSID in the CSV
for that AP. wifi1.1, wifi1.2, and the later subinterfaces are not included.
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import sys
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import urlparse

import requests

PAGE_LIMIT = 100
# /devices/radio-information rejects limit above 50, unlike the shared page size.
RADIO_PAGE_LIMIT = 50
RADIO_BATCH_SIZE = 50
REQUEST_TIMEOUT_SECONDS = 30
BACKOFF_SECONDS = (1, 2, 4, 8, 16)
DEFAULT_BASE_URL = "https://api.extremecloudiq.com"
DEFAULT_RADIO = "wifi1"
UNASSIGNED_SITE = "(unassigned)"
# A fully unresolved breadcrumb is treated as ending on a floor, then a
# building, then a site. XIQ lists ancestors from the root down to the AP,
# and this export sorts floor placements. location_source records the guess.
_FLOOR_TERMINATED_ROLES = ("floor", "building", "site")

COLUMNS = (
    "Site",
    "Building",
    "Floor",
    "AP Name",
    "Serial",
    "Model",
    "Connected",
    "Radio",
    "Radio MAC",
    "WLAN Index",
    "Inferred Subinterface",
    "SSID",
    "BSSID",
    "SSID Status",
    "Network Policy",
    "Location Source",
    "Notes",
)

_HEX_MAC = re.compile(r"[^0-9a-f]")
_NATURAL_SPLIT = re.compile(r"(\d+)")
_RETRY_AFTER_NUMBER = re.compile(r"^\d+(?:\.\d+)?$")


def eprint(message: str) -> None:
    print(message, file=sys.stderr)


def redact(text: str, secrets: Iterable[str]) -> str:
    cleaned = text.replace("\r", " ").replace("\n", " ")
    for secret in secrets:
        if secret:
            cleaned = cleaned.replace(secret, "[redacted]")
    return cleaned


def normalize_mac(value: Any) -> str:
    """Return a lowercase colon-separated MAC, or the trimmed input."""
    if value is None:
        return ""
    text = str(value).strip().lower()
    if not text:
        return ""
    hexdigits = _HEX_MAC.sub("", text)
    if len(hexdigits) == 12:
        return ":".join(hexdigits[i : i + 2] for i in range(0, 12, 2))
    return text


def natural_key(text: str) -> tuple[tuple[int, int | str], ...]:
    """Sort key that orders 'Floor 2' before 'Floor 10'."""
    parts: list[tuple[int, int | str]] = []
    for part in _NATURAL_SPLIT.split(text.casefold()):
        if part.isdigit():
            parts.append((0, int(part)))
        else:
            parts.append((1, part))
    return tuple(parts)


def classify_location_type(type_value: str) -> str:
    """Map an XIQ location type to site, building, floor, or an ignored kind.

    Returns ``unknown`` for values the caller should report once. Site groups
    and the global root are ignored for sorting.
    """
    text = (type_value or "").strip().upper().replace("-", "_").replace(" ", "_")
    if not text:
        return "unknown"
    if text in {"GLOBAL", "ROOT", "GLOBAL_ROOT"}:
        return "root"
    if "SITE_GROUP" in text or text == "SITEGROUP":
        return "site_group"
    if "FLOOR" in text:
        return "floor"
    if "BUILDING" in text:
        return "building"
    if "SITE" in text:
        return "site"
    return "unknown"


def coerce_id(value: Any) -> Any:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        if stripped.lstrip("-").isdigit():
            return int(stripped)
        return stripped
    return value


def as_int(value: Any, default: int | None = None) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def parse_retry_after(header: str, fallback: float) -> float:
    text = header.strip()
    if _RETRY_AFTER_NUMBER.fullmatch(text):
        return max(0.0, float(text))
    try:
        when = parsedate_to_datetime(text)
    except (TypeError, ValueError, OverflowError):
        return fallback
    if when is None:
        return fallback
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    delay = (when - datetime.now(timezone.utc)).total_seconds()
    if delay < 0:
        return 0.0
    # A malformed or multi-hour Retry-After should not stall the export.
    return min(delay, 300.0)


def blank_first(text: str) -> tuple[bool, str]:
    """Empty strings sort before any other value, case-insensitively."""
    return (text != "", text.casefold())


def row_sort_key(row: Mapping[str, str]) -> tuple[Any, ...]:
    index_text = row["WLAN Index"]
    index = int(index_text) if index_text else -1
    return (
        row["Site"].casefold(),
        blank_first(row["Building"]),
        (row["Floor"] != "", natural_key(row["Floor"])),
        natural_key(row["AP Name"]),
        index,
    )


# wifi1.0 uses the radio MAC. wifi1.1, wifi1.2, ... are the following addresses.
# Extreme APs expose at most a handful of SSIDs per radio.
MAX_SUBINTERFACE_OFFSET = 15


def mac_as_int(mac: str) -> int | None:
    text = normalize_mac(mac)
    if len(text) != 17:
        return None
    try:
        return int(text.replace(":", ""), 16)
    except ValueError:
        return None


def subinterface_index(radio_mac: str, bssid: str) -> int | None:
    """Return 0 when the BSSID is the radio MAC, 1 for the next address, and so on."""
    radio = mac_as_int(radio_mac)
    bss = mac_as_int(bssid)
    if radio is None or bss is None:
        return None
    delta = bss - radio
    if 0 <= delta <= MAX_SUBINTERFACE_OFFSET:
        return delta
    return None


def subinterface_warning(radio: str) -> str:
    return (
        f"Warning: only {radio}.0 is written. Its BSSID is the {radio} radio MAC; "
        f"{radio}.1 and later are omitted. The API does not return subinterface names. "
        'Confirm on an AP with "show interface" before trusting that label.'
    )


class XiqClient:
    """Minimal ExtremeCloud IQ client: bearer auth, retries, and paging."""

    def __init__(self, base_url: str, token: str, *, verbose: bool = False) -> None:
        self.base_url = base_url.rstrip("/")
        self._token = token
        self.verbose = verbose
        self.session = requests.Session()
        self.session.headers["Authorization"] = f"Bearer {token}"
        self.session.headers["Accept"] = "application/json"
        self.session.headers["User-Agent"] = "xiq-wifi1-bssid-export"

    def get(self, path: str, params: Sequence[tuple[str, str]] | None = None) -> Any:
        return self._request("GET", path, params=params)

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Sequence[tuple[str, str]] | None = None,
    ) -> Any:
        url = f"{self.base_url}{path}"
        secrets = [self._token]
        attempts = len(BACKOFF_SECONDS) + 1
        for attempt in range(attempts):
            self._log_request(method, url, params)
            try:
                response = self.session.request(
                    method,
                    url,
                    params=params,
                    timeout=REQUEST_TIMEOUT_SECONDS,
                )
            except (requests.Timeout, requests.ConnectionError) as exc:
                if attempt == len(BACKOFF_SECONDS):
                    detail = redact(str(exc), secrets)
                    raise SystemExit(
                        f"request failed after retries: {method} {path}: {detail}"
                    ) from exc
                time.sleep(BACKOFF_SECONDS[attempt])
                continue

            status = response.status_code
            if status == 401:
                raise SystemExit(f"token invalid or expired ({method} {path})")
            if status == 403:
                raise SystemExit(f"token lacks permission ({method} {path})")
            if status == 429 or status >= 500:
                if attempt == len(BACKOFF_SECONDS):
                    raise SystemExit(
                        f"request failed after retries: {method} {path} "
                        f"HTTP {status}"
                    )
                delay = BACKOFF_SECONDS[attempt]
                retry_after = response.headers.get("Retry-After")
                if retry_after:
                    delay = parse_retry_after(retry_after, delay)
                time.sleep(delay)
                continue
            if status >= 400:
                detail = redact(response.text[:300], secrets)
                raise SystemExit(
                    f"request failed: {method} {path} HTTP {status} {detail}"
                )
            if not response.content:
                return None
            try:
                return response.json()
            except ValueError as exc:
                raise SystemExit(
                    f"request failed: {method} {path} returned non-JSON"
                ) from exc
        raise SystemExit(f"request failed after retries: {method} {path}")

    def _log_request(
        self,
        method: str,
        url: str,
        params: Sequence[tuple[str, str]] | None,
    ) -> None:
        if not self.verbose:
            return
        prepared = requests.Request(method, url, params=params).prepare()
        eprint(f"{method} {prepared.url}")

    def iter_pages(
        self,
        path: str,
        extra_params: Sequence[tuple[str, str]] = (),
        *,
        limit: int = PAGE_LIMIT,
    ) -> Iterable[list[dict[str, Any]]]:
        page = 1
        while True:
            params = [("page", str(page)), ("limit", str(limit)), *extra_params]
            payload = self.get(path, params)
            if not isinstance(payload, dict) or "data" not in payload:
                raise SystemExit(f"{path} did not return a paged list")
            data = payload.get("data") or []
            if not isinstance(data, list):
                raise SystemExit(f"{path} returned a data field that is not a list")
            total_pages = as_int(payload.get("total_pages"))
            if self.verbose:
                count = payload.get("count", len(data))
                total_label = total_pages if total_pages is not None else "?"
                eprint(f"{path} page {page}/{total_label} count={count}")
            yield data
            if total_pages is not None and page >= total_pages:
                break
            if not data or (total_pages is None and len(data) < limit):
                break
            page += 1
            if page > 10000:
                raise SystemExit(f"pagination aborted for {path}: too many pages")


def validate_base_url(base_url: str) -> str:
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise SystemExit(f"--base-url must be an http(s) URL, got {base_url!r}")
    return base_url.rstrip("/")


def obtain_token(base_url: str, *, verbose: bool) -> str:
    """Read XIQ_API_TOKEN, or log in when only username and password are set."""
    token = os.environ.get("XIQ_API_TOKEN", "").strip()
    if token:
        if verbose:
            eprint("using bearer token from XIQ_API_TOKEN")
        return token

    username = os.environ.get("XIQ_USERNAME", "").strip()
    password = os.environ.get("XIQ_PASSWORD", "")
    if not username or not password:
        raise SystemExit(
            "XIQ_API_TOKEN is not set. Export a bearer token and retry. "
            "The script sends it as Authorization: Bearer <token> and does "
            "not print it. Alternatively, set both XIQ_USERNAME and "
            "XIQ_PASSWORD to obtain a token from POST /login."
        )

    return _login(base_url, username, password, verbose=verbose)


def _login(base_url: str, username: str, password: str, *, verbose: bool) -> str:
    """POST /login and return access_token. The password is never printed."""
    url = f"{base_url}/login"
    secrets = [password, username]
    attempts = len(BACKOFF_SECONDS) + 1
    for attempt in range(attempts):
        if verbose:
            eprint(f"POST {url}")
        try:
            response = requests.post(
                url,
                json={"username": username, "password": password},
                headers={"Accept": "application/json"},
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
        except (requests.Timeout, requests.ConnectionError) as exc:
            if attempt == len(BACKOFF_SECONDS):
                detail = redact(str(exc), secrets)
                raise SystemExit(f"request failed after retries: POST /login: {detail}") from exc
            time.sleep(BACKOFF_SECONDS[attempt])
            continue

        status = response.status_code
        if status == 401:
            raise SystemExit("login failed: POST /login rejected the username or password")
        if status == 403:
            raise SystemExit("token lacks permission (POST /login)")
        if status == 429 or status >= 500:
            if attempt == len(BACKOFF_SECONDS):
                raise SystemExit(f"request failed after retries: POST /login HTTP {status}")
            delay = BACKOFF_SECONDS[attempt]
            retry_after = response.headers.get("Retry-After")
            if retry_after:
                delay = parse_retry_after(retry_after, delay)
            time.sleep(delay)
            continue
        if status >= 400:
            detail = redact(response.text[:300], secrets)
            raise SystemExit(f"login failed: POST /login HTTP {status} {detail}")
        try:
            body = response.json()
        except ValueError as exc:
            raise SystemExit("login failed: POST /login returned non-JSON") from exc
        access_token = body.get("access_token") if isinstance(body, dict) else None
        if not isinstance(access_token, str) or not access_token.strip():
            raise SystemExit("login response did not include access_token (POST /login)")
        if verbose:
            eprint("authenticated via POST /login")
        return access_token.strip()
    raise SystemExit("request failed after retries: POST /login")


def walk_locations(
    nodes: Any,
    index: dict[Any, dict[str, Any]],
    unknown_types: set[str],
) -> None:
    if isinstance(nodes, dict):
        nodes = [nodes]
    for node in nodes or []:
        if not isinstance(node, dict):
            continue
        loc_id = coerce_id(node.get("id"))
        raw_type = str(node.get("type") or "")
        kind = classify_location_type(raw_type)
        if loc_id is not None:
            index[loc_id] = {
                "name": node.get("name") or "",
                "type": raw_type,
                "parent_id": coerce_id(node.get("parent_id")),
            }
        if kind == "unknown":
            unknown_types.add(raw_type.strip().upper() or "(blank)")
        children = node.get("children") or []
        if children:
            walk_locations(children, index, unknown_types)


def fetch_locations(client: XiqClient) -> tuple[dict[Any, dict[str, Any]], set[str]]:
    payload = client.get("/locations/tree", [("expandChildren", "true")])
    if isinstance(payload, dict) and "total_pages" in payload:
        total_pages = as_int(payload.get("total_pages"), 1) or 1
        if total_pages > 1:
            raise SystemExit(
                "GET /locations/tree returned a paged result; "
                "this script expects the expanded location tree"
            )
        payload = payload.get("data") or []
    if isinstance(payload, dict):
        payload = [payload]
    if not isinstance(payload, list):
        raise SystemExit("GET /locations/tree returned an unexpected payload")
    index: dict[Any, dict[str, Any]] = {}
    unknown: set[str] = set()
    walk_locations(payload, index, unknown)
    if client.verbose:
        eprint(f"/locations/tree locations={len(index)}")
    return index, unknown


def ancestry(
    location_id: Any,
    location_by_id: Mapping[Any, Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], bool]:
    """Walk parent links from a device location back to the root.

    The boolean is true when every parent link was present in the tree.
    """
    chain: list[dict[str, Any]] = []
    seen: set[Any] = set()
    current = coerce_id(location_id)
    complete = True
    while current is not None and current not in seen:
        info = location_by_id.get(current)
        if info is None:
            complete = False
            break
        seen.add(current)
        chain.append({"id": current, "name": info.get("name") or ""})
        parent = coerce_id(info.get("parent_id"))
        if parent is None:
            break
        if parent not in location_by_id:
            complete = False
            break
        current = parent
    chain.reverse()
    return chain, complete


def resolve_location(
    device: Mapping[str, Any],
    location_by_id: Mapping[Any, Mapping[str, Any]],
) -> tuple[str, str, str, str]:
    """Return site, building, floor, and location_source for one device."""
    raw_crumbs = device.get("locations") or []
    crumbs = [crumb for crumb in raw_crumbs if isinstance(crumb, dict)]
    synthesized = False
    chain_complete = True
    if not crumbs:
        crumbs, chain_complete = ancestry(device.get("location_id"), location_by_id)
        synthesized = bool(crumbs)
        if not crumbs:
            return UNASSIGNED_SITE, "", "", "unassigned"

    looked_up: list[tuple[str | None, str]] = []
    any_miss = False
    for crumb in crumbs:
        info = location_by_id.get(coerce_id(crumb.get("id")))
        if info is None:
            any_miss = True
            looked_up.append((None, crumb.get("name") or ""))
        else:
            kind = classify_location_type(str(info.get("type") or ""))
            name = info.get("name") or crumb.get("name") or ""
            looked_up.append((kind, name))

    site, building, floor = _assign_known(looked_up)
    guessed = any_miss or (synthesized and not chain_complete)
    if guessed:
        site, building, floor = _fill_positional(looked_up, site, building, floor)

    if not site and not building and not floor:
        return UNASSIGNED_SITE, "", "", "unassigned"
    source = "breadcrumb-guess" if guessed else "tree"
    return site, building, floor, source


def _assign_known(
    looked_up: Sequence[tuple[str | None, str]],
) -> tuple[str, str, str]:
    site = building = floor = ""
    for kind, name in looked_up:
        if kind == "site":
            site = name
        elif kind == "building":
            building = name
        elif kind == "floor":
            floor = name
    return site, building, floor


def _fill_positional(
    looked_up: Sequence[tuple[str | None, str]],
    site: str,
    building: str,
    floor: str,
) -> tuple[str, str, str]:
    leaf_kind = looked_up[-1][0] if looked_up else None
    if leaf_kind == "building":
        tail: tuple[str, ...] = ("building", "site")
    elif leaf_kind == "site":
        tail = ("site",)
    elif leaf_kind in {"root", "site_group"}:
        tail = ()
    else:
        tail = _FLOOR_TERMINATED_ROLES

    slots = {"site": site, "building": building, "floor": floor}
    role_index = 0
    for kind, name in reversed(looked_up):
        if kind in {"root", "site_group"}:
            continue
        if role_index >= len(tail):
            break
        role = tail[role_index]
        role_index += 1
        if kind is None and not slots[role]:
            slots[role] = name
    return slots["site"], slots["building"], slots["floor"]


def connected_text(device: Mapping[str, Any]) -> str:
    if "connected" not in device or device.get("connected") is None:
        return ""
    return "true" if is_connected(device) else "false"


def is_connected(device: Mapping[str, Any]) -> bool:
    value = device.get("connected")
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value != 0
    if isinstance(value, str):
        return value.strip().casefold() in {"true", "yes", "connected", "1"}
    return False


def fetch_access_points(
    client: XiqClient,
    location_by_id: Mapping[Any, Mapping[str, Any]],
    *,
    site_filter: set[str],
    connected_only: bool,
) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    extra = [("views", "BASIC"), ("views", "LOCATION"), ("async", "false")]
    for page in client.iter_pages("/devices", extra):
        for device in page:
            if not isinstance(device, dict):
                continue
            if str(device.get("device_function") or "").strip() != "AP":
                continue
            if connected_only and not is_connected(device):
                continue
            device_id = coerce_id(device.get("id"))
            if device_id is None:
                continue
            site, building, floor, source = resolve_location(device, location_by_id)
            if site_filter and site.casefold() not in site_filter:
                continue
            selected.append(
                {
                    "id": device_id,
                    "hostname": device.get("hostname") or "",
                    "serial_number": device.get("serial_number") or "",
                    "product_type": device.get("product_type") or "",
                    "connected": connected_text(device),
                    "site": site,
                    "building": building,
                    "floor": floor,
                    "location_source": source,
                }
            )
    return selected


def fetch_radio_map(
    client: XiqClient,
    device_ids: Sequence[Any],
) -> dict[Any, list[dict[str, Any]]]:
    radio_map: dict[Any, list[dict[str, Any]]] = {device_id: [] for device_id in device_ids}
    for start in range(0, len(device_ids), RADIO_BATCH_SIZE):
        batch = device_ids[start : start + RADIO_BATCH_SIZE]
        extra = [("deviceIds", str(device_id)) for device_id in batch]
        for page in client.iter_pages(
            "/devices/radio-information",
            extra,
            limit=RADIO_PAGE_LIMIT,
        ):
            for entity in page:
                if not isinstance(entity, dict):
                    continue
                device_id = coerce_id(entity.get("device_id"))
                if device_id not in radio_map:
                    continue
                radios = entity.get("radios") or []
                if isinstance(radios, dict):
                    radios = [radios]
                if isinstance(radios, list):
                    radio_map[device_id].extend(
                        radio for radio in radios if isinstance(radio, dict)
                    )
    return radio_map


def _base_row(ap: Mapping[str, Any]) -> dict[str, str]:
    return {
        "Site": ap["site"],
        "Building": ap["building"],
        "Floor": ap["floor"],
        "AP Name": ap["hostname"],
        "Serial": ap["serial_number"],
        "Model": ap["product_type"],
        "Connected": ap["connected"],
        "Radio": "",
        "Radio MAC": "",
        "WLAN Index": "",
        "Inferred Subinterface": "",
        "SSID": "",
        "BSSID": "",
        "SSID Status": "",
        "Network Policy": "",
        "Location Source": ap["location_source"],
        "Notes": "",
    }


def _wlan_list(radio: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    wlans = radio.get("wlans") or []
    if isinstance(wlans, dict):
        wlans = [wlans]
    if not isinstance(wlans, list):
        return []
    return [wlan for wlan in wlans if isinstance(wlan, dict)]


def _wlan_row(
    ap: Mapping[str, Any],
    *,
    radio_name: str,
    reported_name: str,
    radio_mac: str,
    wlan: Mapping[str, Any],
    bssid: str,
) -> dict[str, str]:
    ssid = "" if wlan.get("ssid") is None else str(wlan.get("ssid"))
    status = wlan.get("ssid_status")
    policy = wlan.get("network_policy_name")
    row = _base_row(ap)
    row["Radio"] = reported_name
    row["Radio MAC"] = radio_mac
    row["WLAN Index"] = "0"
    row["Inferred Subinterface"] = f"{radio_name}.0"
    row["SSID"] = ssid
    row["BSSID"] = bssid
    row["SSID Status"] = "" if status is None else str(status)
    row["Network Policy"] = "" if policy is None else str(policy)
    return row


def rows_for_ap(
    ap: Mapping[str, Any],
    radios: Sequence[Mapping[str, Any]],
    radio_name: str,
    ssid_filter: set[str],
) -> tuple[list[dict[str, str]], bool]:
    """Return the wifi1.0 row, and whether the AP reported the selected radio.

    wifi1.0 is the BSSID equal to the radio MAC. wifi1.1 and later are dropped.
    """
    wanted = radio_name.casefold()
    matched = [
        radio
        for radio in radios
        if str(radio.get("name") or "").strip().casefold() == wanted
    ]
    if not matched:
        if ssid_filter:
            return [], False
        row = _base_row(ap)
        row["Notes"] = f"no {radio_name} radio reported"
        return [row], False

    rows: list[dict[str, str]] = []
    for radio in matched:
        reported_name = str(radio.get("name") or radio_name).strip() or radio_name
        mac = normalize_mac(radio.get("mac_address") or radio.get("mac"))
        wlans = _wlan_list(radio)
        matched_base = False
        for wlan in wlans:
            ssid = "" if wlan.get("ssid") is None else str(wlan.get("ssid"))
            if ssid_filter and ssid not in ssid_filter:
                continue
            bssid = normalize_mac(wlan.get("bssid"))
            if subinterface_index(mac, bssid) != 0:
                continue
            rows.append(
                _wlan_row(
                    ap,
                    radio_name=radio_name,
                    reported_name=reported_name,
                    radio_mac=mac,
                    wlan=wlan,
                    bssid=bssid,
                )
            )
            matched_base = True
        if matched_base or ssid_filter:
            continue
        row = _base_row(ap)
        row["Radio"] = reported_name
        row["Radio MAC"] = mac
        row["WLAN Index"] = "0"
        row["Inferred Subinterface"] = f"{radio_name}.0"
        row["BSSID"] = mac
        if wlans:
            row["Notes"] = (
                f"no SSID BSSID matched the {radio_name} radio MAC; "
                f"BSSID is the {radio_name}.0 radio address"
            )
        rows.append(row)
    return rows, True


def build_rows(
    access_points: Sequence[Mapping[str, Any]],
    radio_map: Mapping[Any, Sequence[Mapping[str, Any]]],
    radio_name: str,
    ssid_filter: set[str],
) -> tuple[list[dict[str, str]], int]:
    rows: list[dict[str, str]] = []
    with_radio = 0
    for ap in access_points:
        ap_rows, has_radio = rows_for_ap(
            ap,
            radio_map.get(ap["id"], ()),
            radio_name,
            ssid_filter,
        )
        if has_radio:
            with_radio += 1
        rows.extend(ap_rows)
    rows.sort(key=row_sort_key)
    return rows, with_radio


def default_out_path(now: datetime | None = None) -> str:
    stamp = (now or datetime.now()).strftime("%Y%m%d_%H%M")
    return f"xiq_wifi1_bssids_{stamp}.csv"


def write_csv(path: str, rows: Sequence[Mapping[str, str]]) -> None:
    try:
        with open(path, "w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=COLUMNS, extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                writer.writerow(row)
    except OSError as exc:
        raise SystemExit(f"could not write {path}: {exc}") from exc


def print_summary(
    *,
    out_path: str,
    access_points: Sequence[Mapping[str, Any]],
    with_radio: int,
    rows_written: int,
    radio_name: str,
    unknown_types: set[str],
) -> None:
    unassigned = sum(1 for ap in access_points if ap["site"] == UNASSIGNED_SITE)
    unknown = ", ".join(sorted(unknown_types, key=str.casefold)) or "none"
    eprint(f"Output: {out_path}")
    eprint(f"APs: {len(access_points)}")
    eprint(f"APs with {radio_name}: {with_radio}")
    eprint(f"Rows written: {rows_written}")
    eprint(f"APs with no location: {unassigned}")
    eprint(f"Unrecognized location types: {unknown}")
    eprint(subinterface_warning(radio_name))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Export ExtremeCloud IQ AP BSSIDs for one radio to a CSV "
            "sorted by site, building, and floor."
        )
    )
    parser.add_argument(
        "--radio",
        default=DEFAULT_RADIO,
        help=f"Radio name to match (default: {DEFAULT_RADIO})",
    )
    parser.add_argument(
        "--ssid",
        action="append",
        default=[],
        help="SSID to keep. Repeat for more than one. Case-sensitive.",
    )
    parser.add_argument(
        "--site",
        action="append",
        default=[],
        help="Site to keep. Repeat for more than one. Case-insensitive.",
    )
    parser.add_argument(
        "--connected-only",
        action="store_true",
        help="Skip disconnected APs",
    )
    parser.add_argument(
        "--out",
        help="CSV path (default: xiq_wifi1_bssids_YYYYMMDD_HHMM.csv)",
    )
    parser.add_argument(
        "--base-url",
        default=DEFAULT_BASE_URL,
        help=f"API base URL (default: {DEFAULT_BASE_URL})",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Log request URLs without the token, and page counts",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    radio_name = args.radio.strip()
    if not radio_name:
        raise SystemExit("--radio must not be empty")
    base_url = validate_base_url(args.base_url.strip())
    ssid_filter = {ssid for ssid in args.ssid if ssid != ""}
    site_filter = {site.strip().casefold() for site in args.site if site.strip()}
    out_path = args.out or default_out_path()

    token = obtain_token(base_url, verbose=args.verbose)
    client = XiqClient(base_url, token, verbose=args.verbose)
    location_by_id, unknown_types = fetch_locations(client)
    access_points = fetch_access_points(
        client,
        location_by_id,
        site_filter=site_filter,
        connected_only=args.connected_only,
    )
    radio_map = fetch_radio_map(client, [ap["id"] for ap in access_points])
    rows, with_radio = build_rows(access_points, radio_map, radio_name, ssid_filter)
    write_csv(out_path, rows)
    print_summary(
        out_path=out_path,
        access_points=access_points,
        with_radio=with_radio,
        rows_written=len(rows),
        radio_name=radio_name,
        unknown_types=unknown_types,
    )


if __name__ == "__main__":
    main()
