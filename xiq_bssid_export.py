#!/usr/bin/env python3
"""Export chosen ExtremeCloud IQ BSSIDs, sorted by site and floor.

The script reads every access point in an ExtremeCloud IQ organization and
writes one CSV row per access point for each interface in ``config.json``.
The shipped default is ``wifi1.0``, the base BSSID whose last hex digit is 0.
``wifi1.1`` is 4 higher and is the first SSID. Later SSIDs step by one from
there. Rows are sorted by site, building, floor, and AP name.

Setup
-----
Python 3.10 or newer, plus the ``requests`` package::

    python3 -m pip install -r requirements.txt

Put the bearer token in ``.env`` as ``XIQ_API_TOKEN``. The script reads that
file itself and never prints the token. ``config.json`` chooses the
interfaces. See ``AI.md`` for an assistant setting this up on a new machine.

Verification
------------
Confirmed against ``show interface`` on AP-EXAMPLE-01 (XXXXXXXXXXXXXX):
``wifi1.0`` is ``aa:bb:cc:00:00:60`` and ``wifi1.1`` is ``aa:bb:cc:00:00:64``.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Iterable, Mapping, NamedTuple, Sequence
from urllib.parse import urlparse

import requests

PAGE_LIMIT = 100
# /devices/radio-information rejects limit above 50, unlike the shared page size.
RADIO_PAGE_LIMIT = 50
RADIO_BATCH_SIZE = 50
REQUEST_TIMEOUT_SECONDS = 30
BACKOFF_SECONDS = (1, 2, 4, 8, 16)
DEFAULT_BASE_URL = "https://api.extremecloudiq.com"
DEFAULT_CONFIG_PATH = "config.json"
DEFAULT_INTERFACE = "wifi1.0"
# wifiN.1 sits at base+4. wifiN.12 is the last address in the 16-wide block.
MAX_BSS_INDEX = 12
UNASSIGNED_SITE = "(unassigned)"
_CONFIG_KEYS = {"interfaces", "ssids", "sites", "connected_only", "base_url", "out"}
_DOTENV_KEYS = {"XIQ_API_TOKEN", "XIQ_USERNAME", "XIQ_PASSWORD"}
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
_INTERFACE = re.compile(r"^(wifi\d+)\.(\d+)$", re.IGNORECASE)


class InterfaceChoice(NamedTuple):
    radio: str
    bss: int
    label: str


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


def mac_as_int(mac: str) -> int | None:
    text = normalize_mac(mac)
    if len(text) != 17:
        return None
    try:
        return int(text.replace(":", ""), 16)
    except ValueError:
        return None


def format_mac(value: int) -> str:
    text = f"{value:012x}"
    return ":".join(text[i : i + 2] for i in range(0, 12, 2))


def interface_mac_for_radio(api_mac: str) -> tuple[str, int | None]:
    """Map an API radio MAC to the ``show interface`` WifiN address.

    IQ Engine gives each radio a 16-address block whose base ends in 0.
    That base is WifiN and carries no SSID. WifiN.1 is base + 4, WifiN.2
    is base + 5, and so on. ``mac_address`` from radio-information is the
    base when no SSID is up, and WifiN.1 when one is.

    The returned index is 0 when ``api_mac`` is already WifiN, 1 for
    WifiN.1, and so on. None means the address is outside that pattern
    and was left unchanged.
    """
    normalized = normalize_mac(api_mac)
    value = mac_as_int(normalized)
    if value is None:
        return normalized, None
    offset = value & 0xF
    if offset == 0:
        return normalized, 0
    if offset >= 4:
        return format_mac(value & ~0xF), offset - 3
    return normalized, None


def bssid_for_index(interface_mac: str, bss: int) -> str:
    """Return wifiN.0 at the block base, wifiN.1 at base + 4, then one each."""
    value = mac_as_int(interface_mac)
    if value is None:
        return ""
    if bss == 0:
        return format_mac(value)
    return format_mac(value + 3 + bss)


def parse_interface(text: str) -> InterfaceChoice:
    match = _INTERFACE.fullmatch(text.strip())
    if match is None:
        raise SystemExit(
            f"interface must look like {DEFAULT_INTERFACE}, got {text!r}"
        )
    radio = match.group(1).lower()
    bss = int(match.group(2))
    if bss > MAX_BSS_INDEX:
        raise SystemExit(
            f"{text} is outside the radio block "
            f"(wifiN.0 through wifiN.{MAX_BSS_INDEX})"
        )
    return InterfaceChoice(radio, bss, f"{radio}.{bss}")


def parse_interface_list(values: Sequence[str]) -> list[InterfaceChoice]:
    choices: list[InterfaceChoice] = []
    seen: set[str] = set()
    for value in values:
        choice = parse_interface(value)
        if choice.label in seen:
            continue
        seen.add(choice.label)
        choices.append(choice)
    if not choices:
        raise SystemExit(
            f"list at least one interface, such as {DEFAULT_INTERFACE}"
        )
    return choices


def _string_list(value: Any, field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise SystemExit(f"config {field} must be a list of strings")
    return [item for item in value if item.strip()]


def default_settings() -> dict[str, Any]:
    return {
        "interfaces": parse_interface_list([DEFAULT_INTERFACE]),
        "ssids": [],
        "sites": [],
        "connected_only": False,
        "base_url": DEFAULT_BASE_URL,
        "out": "",
    }


def load_config(path: str) -> dict[str, Any]:
    """Read config.json. A missing default file falls back to wifi1.0."""
    settings = default_settings()
    if not os.path.isfile(path):
        if os.path.normcase(os.path.normpath(path)) == os.path.normcase(
            os.path.normpath(DEFAULT_CONFIG_PATH)
        ):
            eprint(f"{path} not found; using {DEFAULT_INTERFACE}")
            return settings
        raise SystemExit(f"config file not found: {path}")
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except OSError as exc:
        raise SystemExit(f"could not read {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise SystemExit(f"could not parse {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise SystemExit(f"{path} must contain a JSON object")
    unknown = sorted(set(payload) - _CONFIG_KEYS)
    if unknown:
        raise SystemExit(
            f"{path} has unknown keys: {', '.join(unknown)}. "
            f"Expected: {', '.join(sorted(_CONFIG_KEYS))}"
        )
    if "interfaces" in payload:
        interfaces = payload["interfaces"]
        if (
            not isinstance(interfaces, list)
            or not all(isinstance(item, str) for item in interfaces)
        ):
            raise SystemExit("config interfaces must be a list of strings")
        settings["interfaces"] = parse_interface_list(interfaces)
    settings["ssids"] = _string_list(payload.get("ssids", []), "ssids")
    settings["sites"] = _string_list(payload.get("sites", []), "sites")
    if "connected_only" in payload and not isinstance(payload["connected_only"], bool):
        raise SystemExit("config connected_only must be true or false")
    settings["connected_only"] = bool(payload.get("connected_only", False))
    if "base_url" in payload:
        if not isinstance(payload["base_url"], str) or not payload["base_url"].strip():
            raise SystemExit("config base_url must be a URL string")
        settings["base_url"] = payload["base_url"].strip()
    if "out" in payload and payload["out"] is not None:
        if not isinstance(payload["out"], str):
            raise SystemExit("config out must be a file path string")
        settings["out"] = payload["out"].strip()
    return settings


def load_dotenv(path: str = ".env") -> None:
    """Fill unset XIQ_* variables from .env. Values are never printed."""
    if not os.path.isfile(path):
        return
    try:
        lines = open(path, encoding="utf-8").read().splitlines()
    except OSError as exc:
        raise SystemExit(f"could not read {path}: {exc}") from exc
    for line in lines:
        text = line.strip()
        if not text or text.startswith("#") or "=" not in text:
            continue
        key, value = text.split("=", 1)
        key = key.strip()
        if key not in _DOTENV_KEYS:
            continue
        if os.environ.get(key, "").strip():
            continue
        os.environ[key] = value.strip().strip('"').strip("'")


def interface_warning(choices: Sequence[InterfaceChoice]) -> str:
    labels = ", ".join(choice.label for choice in choices)
    return (
        f"Exporting {labels}. "
        "wifiN.0 is the base BSSID (last hex digit 0). "
        "wifiN.1 is 4 higher. Each later BSS is one higher than wifiN.1."
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
        self.session.headers["User-Agent"] = "xiq-bssid-export"

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
    load_dotenv()
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


def _ssid_name(wlan: Mapping[str, Any]) -> str:
    ssid = wlan.get("ssid")
    return "" if ssid is None else str(ssid)


def _wlan_by_bssid(
    radio: Mapping[str, Any],
) -> dict[str, Mapping[str, Any]]:
    found: dict[str, Mapping[str, Any]] = {}
    for wlan in _wlan_list(radio):
        bssid = normalize_mac(wlan.get("bssid"))
        if bssid:
            found[bssid] = wlan
    return found


def _choice_row(
    ap: Mapping[str, Any],
    *,
    reported_name: str,
    choice: InterfaceChoice,
    interface_mac: str,
    api_mac: str,
    api_index: int | None,
    wlan: Mapping[str, Any] | None,
    bssid: str,
) -> dict[str, str]:
    row = _base_row(ap)
    row["Radio"] = reported_name
    row["Radio MAC"] = interface_mac
    row["WLAN Index"] = str(choice.bss)
    row["Inferred Subinterface"] = f"{reported_name}.{choice.bss}"
    row["BSSID"] = bssid
    if wlan is not None:
        status = wlan.get("ssid_status")
        policy = wlan.get("network_policy_name")
        row["SSID"] = _ssid_name(wlan)
        row["SSID Status"] = "" if status is None else str(status)
        row["Network Policy"] = "" if policy is None else str(policy)
    notes: list[str] = []
    if not interface_mac:
        notes.append(f"no {choice.radio} MAC reported")
    elif api_index is None and api_mac:
        notes.append(
            f"API radio MAC {api_mac} is outside the {reported_name} address block"
        )
        if choice.bss > 0:
            notes.append(f"could not place {reported_name}.{choice.bss}")
    elif choice.bss == 0 and api_index not in (None, 0):
        notes.append(f"API radio MAC {api_mac} is {reported_name}.{api_index}")
    elif choice.bss > 0 and wlan is None and bssid:
        notes.append(f"no SSID reported on {reported_name}.{choice.bss}")
    row["Notes"] = "; ".join(notes)
    return row


def rows_for_ap(
    ap: Mapping[str, Any],
    radios: Sequence[Mapping[str, Any]],
    choices: Sequence[InterfaceChoice],
    ssid_filter: set[str],
) -> tuple[list[dict[str, str]], bool]:
    """Return one row per requested interface, and whether any radio existed."""
    grouped: dict[str, list[InterfaceChoice]] = {}
    for choice in choices:
        grouped.setdefault(choice.radio.casefold(), []).append(choice)

    rows: list[dict[str, str]] = []
    any_radio = False
    for radio_key, radio_choices in grouped.items():
        matched = [
            radio
            for radio in radios
            if str(radio.get("name") or "").strip().casefold() == radio_key
        ]
        if not matched:
            if ssid_filter:
                continue
            for choice in radio_choices:
                row = _base_row(ap)
                row["Radio"] = choice.radio
                row["WLAN Index"] = str(choice.bss)
                row["Inferred Subinterface"] = choice.label
                row["Notes"] = f"no {choice.radio} radio reported"
                rows.append(row)
            continue
        any_radio = True
        for radio in matched:
            reported_name = str(radio.get("name") or radio_key).strip() or radio_key
            api_mac = normalize_mac(radio.get("mac_address") or radio.get("mac"))
            interface_mac, api_index = interface_mac_for_radio(api_mac)
            wlan_by_bssid = _wlan_by_bssid(radio)
            for choice in radio_choices:
                if api_index is None:
                    bssid = interface_mac if choice.bss == 0 else ""
                else:
                    bssid = bssid_for_index(interface_mac, choice.bss)
                wlan = wlan_by_bssid.get(bssid)
                if ssid_filter and (wlan is None or _ssid_name(wlan) not in ssid_filter):
                    continue
                rows.append(
                    _choice_row(
                        ap,
                        reported_name=reported_name,
                        choice=choice,
                        interface_mac=interface_mac,
                        api_mac=api_mac,
                        api_index=api_index,
                        wlan=wlan,
                        bssid=bssid,
                    )
                )
    return rows, any_radio


def build_rows(
    access_points: Sequence[Mapping[str, Any]],
    radio_map: Mapping[Any, Sequence[Mapping[str, Any]]],
    choices: Sequence[InterfaceChoice],
    ssid_filter: set[str],
) -> tuple[list[dict[str, str]], int]:
    rows: list[dict[str, str]] = []
    with_radio = 0
    for ap in access_points:
        ap_rows, has_radio = rows_for_ap(
            ap,
            radio_map.get(ap["id"], ()),
            choices,
            ssid_filter,
        )
        if has_radio:
            with_radio += 1
        rows.extend(ap_rows)
    rows.sort(key=row_sort_key)
    return rows, with_radio


def default_out_path(now: datetime | None = None) -> str:
    stamp = (now or datetime.now()).strftime("%Y%m%d_%H%M")
    return f"xiq_bssids_{stamp}.csv"


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
    choices: Sequence[InterfaceChoice],
    unknown_types: set[str],
) -> None:
    unassigned = sum(1 for ap in access_points if ap["site"] == UNASSIGNED_SITE)
    unknown = ", ".join(sorted(unknown_types, key=str.casefold)) or "none"
    radios = ", ".join(dict.fromkeys(choice.radio for choice in choices))
    eprint(f"Output: {out_path}")
    eprint(f"APs: {len(access_points)}")
    eprint(f"APs with {radios}: {with_radio}")
    eprint(f"Rows written: {rows_written}")
    eprint(f"APs with no location: {unassigned}")
    eprint(f"Unrecognized location types: {unknown}")
    eprint(interface_warning(choices))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Export chosen ExtremeCloud IQ BSSIDs to a CSV "
            "sorted by site, building, and floor."
        )
    )
    parser.add_argument(
        "--config",
        default=DEFAULT_CONFIG_PATH,
        help=f"JSON settings file (default: {DEFAULT_CONFIG_PATH})",
    )
    parser.add_argument(
        "--interface",
        action="append",
        default=None,
        help=(
            "Interface to export, such as wifi1.0 or wifi1.1. "
            "Repeat to export more than one. Replaces the config.json list."
        ),
    )
    parser.add_argument(
        "--ssid",
        action="append",
        default=None,
        help=(
            "SSID to keep. Repeat for more than one. Case-sensitive. "
            "Replaces the config.json list. wifiN.0 has no SSID, so a filter drops it."
        ),
    )
    parser.add_argument(
        "--site",
        action="append",
        default=None,
        help=(
            "Site to keep. Repeat for more than one. Case-insensitive. "
            "Replaces the config.json list."
        ),
    )
    parser.add_argument(
        "--connected-only",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Skip disconnected APs. Overrides config.json.",
    )
    parser.add_argument(
        "--out",
        help="CSV path. Overrides config.json. Default: xiq_bssids_YYYYMMDD_HHMM.csv",
    )
    parser.add_argument(
        "--base-url",
        default=None,
        help=f"API base URL. Overrides config.json (default: {DEFAULT_BASE_URL}).",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Log request URLs without the token, and page counts",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    settings = load_config(args.config)
    choices = (
        parse_interface_list(args.interface)
        if args.interface is not None
        else settings["interfaces"]
    )
    base_url = validate_base_url((args.base_url or settings["base_url"]).strip())
    ssid_values = settings["ssids"] if args.ssid is None else args.ssid
    site_values = settings["sites"] if args.site is None else args.site
    ssid_filter = {ssid for ssid in ssid_values if ssid != ""}
    site_filter = {site.strip().casefold() for site in site_values if site.strip()}
    connected_only = (
        settings["connected_only"]
        if args.connected_only is None
        else args.connected_only
    )
    out_path = args.out or settings["out"] or default_out_path()

    token = obtain_token(base_url, verbose=args.verbose)
    client = XiqClient(base_url, token, verbose=args.verbose)
    location_by_id, unknown_types = fetch_locations(client)
    access_points = fetch_access_points(
        client,
        location_by_id,
        site_filter=site_filter,
        connected_only=connected_only,
    )
    radio_map = fetch_radio_map(client, [ap["id"] for ap in access_points])
    rows, with_radio = build_rows(access_points, radio_map, choices, ssid_filter)
    write_csv(out_path, rows)
    print_summary(
        out_path=out_path,
        access_points=access_points,
        with_radio=with_radio,
        rows_written=len(rows),
        choices=choices,
        unknown_types=unknown_types,
    )


if __name__ == "__main__":
    main()
