#!/usr/bin/env python3
"""Pull Chrome Web Store usage analytics for your extension to CSV.

The Chrome Web Store has no public analytics API. Its developer dashboard reads stats from an
internal Google RPC gateway (/_/SnapcatUi/data/batchexecute). This tool sends the same
requests with your logged-in cookie and writes CSVs:

  - weekly active users, split by country, language, OS, version, enabled state, install source
  - installs and uninstalls, split by country, language, OS, and install source
  - total impressions and detail-page views
  - page views by UTM source, medium, and campaign
  - ratings, as per-day star counts

Authentication. You give it your dashboard cookie and the two IDs from the dashboard URL. The
script mints the short-lived XSRF token and build label from the dashboard page, and picks up
rotated session cookies from responses, so the cookie is the only thing you keep current. When
the cookie expires, paste a fresh one.

Configuration in .env (see .env.example):
  CWS_EXTENSION_ID   extension id, 32 chars, from the dashboard URL
  CWS_ACCOUNT_ID     publisher id, a UUID, from the dashboard URL
  CWS_COOKIE         the Cookie header your browser sends to chrome.google.com

Command line:
  cws_analytics.py                  fetch all-time
  cws_analytics.py --since 2026-01-01
  cws_analytics.py --list           list every extension on the account
  cws_analytics.py --refresh-token  mint a fresh token and exit

As a library:
  from cws_analytics import CwsClient
  client = CwsClient.from_env()
  series = client.stats()            # {"weekly_users": {date: n}, "installs_by_country": ...}
  users = client.weekly_users()      # {date: n}

Python 3.9+, standard library only. This is unofficial and breaks if Google changes the
dashboard. Use it for extensions you own. Not affiliated with Google.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import stat
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from typing import Optional

__version__ = "1.0.0"
__all__ = ["CwsClient", "Session", "CwsError"]

# --------------------------------------------------------------------------- endpoints
BATCHEXECUTE = "https://chrome.google.com/_/SnapcatUi/data/batchexecute"
DASHBOARD_PAGE = "https://chrome.google.com/webstore/devconsole/{account}/{ext}/analytics/users"
USER_AGENT = "Mozilla/5.0"
ENV_PATH = ".env"

# Wire-ids for the four RPCs this tool calls. These ids are build-specific: Google's boq build
# mints them (see the bl/cfb2h build label) and they are identical for every account on a given
# build, but a future dashboard rebuild can change them. If calls start failing, Google rebuilt
# the dashboard; re-derive the ids from the page's JS bundle, where they are registered as
# new _.Mv("<id>", ..., "/<Service>.<Method>"), and update them here. They are not secret and
# not account-specific.
RPC_GET_ITEM_STATS = "WlSRsc"       # GetItemStats: usage time-series
RPC_GET_ITEM_RATINGS = "vjWKuf"     # GetItemRatings: per-day star buckets
RPC_GET_ITEM = "Mt2BP"              # GetItem: name, version, category
RPC_GET_PUBLISHER_ITEMS = "mE4CQe"  # GetPublisherItems: all items under an account

# Reference only, not called by this tool. The full set of developer-dashboard RPCs found in
# the JS bundle, recorded so the surface is documented rather than lost. kind: data = returns
# analytics or records, meta = item/account metadata, write = mutates listing state,
# config = static UI config. The four reads above are the only ones this tool uses.
KNOWN_RPCS = {
    # SnapcatItemDataService (per extension)
    "WlSRsc": ("GetItemStats", "data"), "vjWKuf": ("GetItemRatings", "data"),
    "Mt2BP": ("GetItem", "meta"), "mE4CQe": ("GetPublisherItems", "meta"),
    "LsTwkc": ("GetItemSupportIssues", "data"), "AR7avc": ("GetActiveAppeal", "data"),
    "Gloc4c": ("GetPublishItemError", "data"), "y8nVDc": ("GetItemTestCredentials", "config"),
    "Z3PXjf": ("GetNewCategories", "config"), "ezONlb": ("GetSupportedLanguages", "config"),
    "csnm0": ("PublishItem", "write"), "IGDNge": ("CancelPublishItem", "write"),
    "K9P69e": ("CancelSubmission", "write"), "Z63YId": ("UnpublishItem", "write"),
    "RmG6ad": ("ArchiveItem", "write"), "Vddhq": ("UnarchiveItem", "write"),
    "qhj82d": ("SaveItemDraft", "write"), "fNXSJd": ("UpdatePublishedDeployInfo", "write"),
    "rrqUMd": ("SaveItemTestCredentials", "write"), "AaxNP": ("DeleteMedia", "write"),
    "ZtNYHd": ("TransferItemToGroupPublisher", "write"),
    "T148Qc": ("OptInToVerifiedCrxUpload", "write"),
    "UDx6Zc": ("OptInToGoogleAnalytics", "write"), "F9Xvab": ("OptOutOfGoogleAnalytics", "write"),
    # SnapcatPublisherDataService (per account)
    "Kgaqm": ("GetDeveloper", "meta"), "vK2CA": ("GetPublisherDetails", "meta"),
    "SI1TOd": ("GetRoleAndPermissions", "data"), "K7KxXd": ("GetAlerts", "data"),
    "ZDP8Sb": ("GetUploadAttempts", "data"), "gKbBfb": ("GetGroupPublishers", "meta"),
    "vzA4z": ("GetCreatedPublishers", "meta"), "VNRKbf": ("GetOwnedSites", "meta"),
    "Z3vGpd": ("ListPublisherMemberships", "meta"), "wp3ZC": ("ListMemberInvitations", "meta"),
    "ZO1Ut": ("GetInvitation", "meta"), "kU2C6e": ("GetCurrentUserDasherInfo", "config"),
    "I7VbId": ("GetPublisherForOrganizationApproval", "config"),
    "ZPTX7c": ("UpdateDeveloperAlert", "write"),
    # NotificationsApiService
    "BLZoCe": ("FetchLatestThreads", "data"), "fcRCpc": ("FetchUserPreferences", "config"),
    "W5Udfe": ("FetchTargetGroupPreferences", "config"),
}

# Non-batchexecute endpoints seen in the bundle/captures (not used here, recorded for reference):
#   POST /webstore/developer/uploadv4          media/screenshot upload
#   POST /_/SnapcatUi/browserinfo              client telemetry ping
#   POST /_/SnapcatUi/web-reports              browser deprecation/error reports
#   POST accounts.google.com/RotateCookies     session cookie rotation (a background keep-alive could use this)
# OneGoogle account-bar RPCs (WidgetService: GetAccountMenuModel/GetAppWidgetModel/...) and the
# gRPC OneGoogle AsyncDataService are page chrome, unrelated to Chrome Web Store data.

# --------------------------------------------------------------------- GetItemStats codes
# A GetItemStats series is labelled in the response only by its DIMENSION (e.g. "COUNTRY"),
# not by its metric group, so users-by-country and installs-by-country both come back as
# "COUNTRY". We therefore tag each series by the (group, dim) of its request (see
# decode_stats). Codes verified against the dashboard bundle and live responses.
GROUP_NAMES = {1: "installs", 2: "uninstalls", 3: "impressions", 4: "weekly_users", 7: "pageviews"}
DIM_NAMES = {1: "os", 2: "version", 3: "country", 4: "install_source", 5: "language", 6: "enabled",
             7: "utm_campaign", 8: "utm_source", 9: "utm_medium"}

# The (group, dim) pairs to request. dim is None means scalar total. The dashboard UI itself
# requests all of these except the dim=4 (install_source) breakdowns, which the server returns
# but no dashboard tab shows. install_source separates store installs from "Other" (managed or
# off-store deployment), which the weekly-users count otherwise hides.
STATS_QUERIES: list[tuple[int, Optional[int]]] = [
    (4, None), (4, 1), (4, 2), (4, 3), (4, 4), (4, 5), (4, 6),  # weekly users + composition (+install_source)
    (1, None), (1, 1), (1, 3), (1, 4), (1, 5),                  # installs total + by os/country/source/language
    (2, None), (2, 1), (2, 3), (2, 4), (2, 5),                  # uninstalls total + by os/country/source/language
    (3, None),                                                  # impressions
    (7, None), (7, 7), (7, 8), (7, 9),                          # page views + utm campaign/source/medium
]


class CwsError(Exception):
    """Raised for configuration and request failures (expired cookie, missing .env keys)."""


# ============================================================================ config
class Session:
    """Live credentials and request context, loaded from .env.

    Only cookie, ext and account are required. at (XSRF token) and bl (build label) are minted
    from the dashboard page when absent or stale, so they normally stay blank in .env.
    """

    def __init__(self, env_path: str = ENV_PATH) -> None:
        self.env_path = env_path
        env = _read_env(env_path)
        self.cookie = _require(env, "CWS_COOKIE")
        self.ext = _require(env, "CWS_EXTENSION_ID")
        self.account = _require(env, "CWS_ACCOUNT_ID")
        if not re.fullmatch(r"[a-p]{32}", self.ext):
            raise CwsError("CWS_EXTENSION_ID should be 32 chars a-p (copy it from the dashboard URL).")
        if not re.fullmatch(r"[0-9a-f-]{36}", self.account):
            raise CwsError("CWS_ACCOUNT_ID should be a UUID (copy it from the dashboard URL).")
        self.at = env.get("CWS_AT", "")
        self.bl = env.get("CWS_BL", "")
        self.sid = env.get("CWS_SID", "")
        self.hl = env.get("CWS_HL", "en")


def _read_env(path: str) -> dict[str, str]:
    env: dict[str, str] = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, val = line.split("=", 1)
                    env[key.strip()] = val.strip()
    return env


def _require(env: dict[str, str], key: str) -> str:
    val = env.get(key)
    if not val:
        raise CwsError(f"Missing {key} in {ENV_PATH} (see .env.example).")
    return val


def update_env(path: str, updates: dict[str, str]) -> None:
    """Rewrite specific keys in .env, preserving everything else. Writes atomically with mode
    0600 so the cookie and token never touch disk world/group-readable, and a crash mid-write
    cannot leave a truncated .env that loses the session."""
    lines: list[str] = []
    seen: set[str] = set()
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                key = line.split("=", 1)[0].strip()
                if key in updates and not line.lstrip().startswith("#"):
                    lines.append(f"{key}={updates[key]}\n")
                    seen.add(key)
                else:
                    lines.append(line if line.endswith("\n") else line + "\n")
    for key, val in updates.items():
        if key not in seen:
            lines.append(f"{key}={val}\n")
    directory = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".env.", suffix=".tmp")
    try:
        os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)  # 0600 before any secret is written
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.writelines(lines)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        finally:
            raise


def _loads(value):
    """json.loads a wrb.fr payload, tolerating an already-decoded value or bad JSON."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return None
    return value


# ============================================================ request encoding (pure)
def _compact(obj: object) -> str:
    return json.dumps(obj, separators=(",", ":"))


def build_freq(calls: list[tuple[str, str]]) -> str:
    """Wrap (rpc_id, inner_json) pairs into a boq f.req envelope.

    Sub-requests are numbered with odd indices (3, 5, 7, ...). The response echoes that index,
    which is how decode_stats maps each series back to its request.
    """
    arr = []
    idx = 1
    for rpc_id, inner in calls:
        idx += 2
        arr.append([rpc_id, inner, None, str(idx)])
    return _compact([arr])


def stats_inner(ext: str, group: int, dim: Optional[int], day_start: int, day_end: int) -> str:
    args: list = [[None, [day_start, day_end]], ext, group]
    if dim is not None:
        args.append(dim)
    return _compact([args])


def ratings_inner(ext: str, day_start: int, day_end: int) -> str:
    return _compact([[[None, [day_start, day_end]], ext]])


# ============================================================ response decoding (pure)
def _iter_chunks(text: str):
    """Yield the JSON arrays from a batchexecute response body.

    Two framings exist. The default (rt=c) is length-prefixed: a decimal line, then one
    minified JSON array per line, repeated. The other (rt=default) is a single JSON array with
    no prefixes. We handle the single-array form (including pretty-printed) by parsing the whole
    body when it starts with "[", and the length-prefixed form line by line otherwise. The
    decimal length lines are used only as delimiters, so their exact value never matters.
    """
    if text.startswith(")]}'"):
        text = text[4:]
    if text.lstrip().startswith("["):          # single-array framing (any whitespace)
        try:
            yield json.loads(text)
            return
        except json.JSONDecodeError:
            pass
    buf = ""
    for line in text.split("\n"):              # length-prefixed framing
        if line.strip().isdigit():
            if buf.strip():
                try:
                    yield json.loads(buf)
                except json.JSONDecodeError:
                    pass
            buf = ""
        else:
            buf += line + "\n"
    if buf.strip():
        try:
            yield json.loads(buf)
        except json.JSONDecodeError:
            pass


def first_payload(text: str, rpc_id: str) -> Optional[object]:
    """Return the first decoded wrb.fr payload for rpc_id, or None."""
    for chunk in _iter_chunks(text):
        if not isinstance(chunk, list):
            continue
        for row in chunk:
            if (isinstance(row, list) and len(row) > 2 and row[0] == "wrb.fr"
                    and row[1] == rpc_id and row[2]):
                return _loads(row[2])
    return None


def _payload_series(payload, is_dim: bool) -> dict:
    """One GetItemStats payload -> {date: count} (scalar) or {date: {label: count}} (dim)."""
    out: dict = {}

    def walk(node, current: Optional[str] = None) -> None:
        if not isinstance(node, list):
            return
        if node and isinstance(node[0], str) and len(node[0]) == 10 and node[0][4] == "-":
            current = node[0]
            if is_dim:
                out.setdefault(current, {})
            for child in node[1:]:
                walk(child, current)
            return
        if len(node) == 3 and isinstance(node[1], int) and isinstance(node[2], str):
            if is_dim and isinstance(node[0], str) and current is not None:
                out[current][node[2]] = node[1]          # ["COUNTRY", count, "Germany"]
                return
            if not is_dim and current is not None:
                out[current] = node[1]                   # [code, count, "WEEKLY_USERS"]
                return
        for child in node:
            walk(child, current)

    walk(payload)
    return out


def decode_stats(text: str, idx_to_query: dict[str, tuple[int, Optional[int]]]) -> dict[str, dict]:
    """Decode a GetItemStats batch into {qualified_name: series}, tagging each response by the
    (group, dim) of its request via the echoed sub-request index."""
    payload_for: dict[str, object] = {}
    for chunk in _iter_chunks(text):
        if not isinstance(chunk, list):
            continue
        for row in chunk:
            if (isinstance(row, list) and len(row) > 6 and row[0] == "wrb.fr"
                    and row[1] == RPC_GET_ITEM_STATS and row[2]):
                payload_for[row[6]] = _loads(row[2])
    series: dict[str, dict] = {}
    for idx, (group, dim) in idx_to_query.items():
        if idx not in payload_for:
            continue
        name = GROUP_NAMES.get(group, f"g{group}")
        if dim is not None:
            name += f"_by_{DIM_NAMES.get(dim, f'd{dim}')}"
        series[name] = _payload_series(payload_for[idx], dim is not None)
    return series


def parse_ratings(text: str) -> dict[str, list[int]]:
    """GetItemRatings response -> {date: [1star..5star]} (deduped per date)."""
    stars: dict[str, list[int]] = {}
    payload = first_payload(text, RPC_GET_ITEM_RATINGS)
    if not (isinstance(payload, list) and len(payload) >= 2 and payload[1]):
        return stars
    for row in payload[1]:
        if not (isinstance(row, list) and len(row) >= 2):
            continue
        arr = row[-1] if isinstance(row[-1], list) else []
        if any(arr):
            vals = [v or 0 for v in arr[:5]]
            stars[row[0]] = vals + [0] * (5 - len(vals))   # pad short buckets to 5
    return stars


# ============================================================================ client
def _has_data(status: Optional[int], text: str) -> bool:
    # The gateway can wrap an embedded ["er", ..., 401] in an HTTP 200 with no "wrb.fr" rows,
    # so the presence of a wrb.fr envelope is the real success test, not the status code.
    return status == 200 and '"wrb.fr"' in text


def _require_data(status: Optional[int], text: str) -> None:
    """Raise the right CwsError when a response carried no usable data."""
    if status is None:
        raise CwsError(f"Could not reach Chrome Web Store ({text}).")
    if not _has_data(status, text):
        raise CwsError("No data returned. The cookie has likely expired; refresh CWS_COOKIE in .env.")


class CwsClient:
    """Authenticated client for the Chrome Web Store developer dashboard RPCs.

    Holds a Session, sends batchexecute requests, refreshes the XSRF token from the dashboard
    page when it goes stale, and absorbs rotated session cookies back into .env.
    """

    def __init__(self, session: Session) -> None:
        self.session = session

    @classmethod
    def from_env(cls, env_path: str = ENV_PATH) -> "CwsClient":
        return cls(Session(env_path))

    # ---- transport ----
    def _call(self, rpc_ids: str, freq: str) -> tuple[Optional[int], str]:
        s = self.session
        params = {
            "rpcids": rpc_ids,
            "source-path": f"/webstore/devconsole/{s.account}/{s.ext}/analytics",
            "f.sid": s.sid, "bl": s.bl, "hl": s.hl,
            "soc-app": "630", "soc-platform": "1", "soc-device": "1", "rt": "c",
        }
        url = BATCHEXECUTE + "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v})
        body = urllib.parse.urlencode({"f.req": freq, "at": s.at}).encode()
        req = urllib.request.Request(url, data=body, method="POST", headers={
            "content-type": "application/x-www-form-urlencoded;charset=UTF-8",
            "x-same-domain": "1", "origin": "https://chrome.google.com", "cookie": s.cookie,
        })
        try:
            resp = urllib.request.urlopen(req, timeout=30)
            text = resp.read().decode("utf-8", "replace")
            self._absorb_set_cookie(resp)
            return resp.status, text
        except urllib.error.HTTPError as exc:
            text = exc.read().decode("utf-8", "replace")
            self._absorb_set_cookie(exc)
            return exc.code, text
        except urllib.error.URLError as exc:
            return None, f"network error: {exc.reason}"
        except OSError as exc:   # socket timeout and other low-level transport errors
            return None, f"network error: {exc}"

    def _call_with_refresh(self, rpc_ids: str, freq: str) -> tuple[Optional[int], str]:
        """Call, and if the token is stale, re-mint it from the page and retry once."""
        status, text = self._call(rpc_ids, freq)
        if not _has_data(status, text) and self.refresh_token():
            status, text = self._call(rpc_ids, freq)
        return status, text

    def _absorb_set_cookie(self, resp) -> None:
        """Merge rotated cookies from a response into the session jar and .env.

        Google re-issues short-lived binding cookies (__Secure-1PSIDTS / 3PSIDTS / SIDCC) on
        responses. Capturing them keeps the session alive as long as the durable identity
        cookies last, instead of dying when the original snapshot goes stale.
        """
        raw = resp.headers.get_all("Set-Cookie") if resp is not None else None
        if not raw:
            return
        jar: dict[str, str] = {}
        for part in self.session.cookie.split(";"):
            part = part.strip()
            if "=" in part:
                key, val = part.split("=", 1)
                jar[key] = val
        changed = False
        for set_cookie in raw:
            name_val = set_cookie.split(";", 1)[0].strip()
            if "=" not in name_val:
                continue
            key, val = name_val.split("=", 1)
            if val and val.lower() not in ("deleted", "expired") and jar.get(key) != val:
                jar[key] = val
                changed = True
        if changed:
            self.session.cookie = "; ".join(f"{k}={v}" for k, v in jar.items())
            update_env(self.session.env_path, {"CWS_COOKIE": self.session.cookie})

    def refresh_token(self) -> bool:
        """Mint a fresh XSRF token (SNlM0e) and build label (cfb2h) from the dashboard page,
        persisting them to .env. Returns True on success. Fails when the cookie has expired."""
        s = self.session
        url = DASHBOARD_PAGE.format(account=s.account, ext=s.ext)
        req = urllib.request.Request(url, headers={
            "cookie": s.cookie, "user-agent": USER_AGENT, "accept": "text/html"})
        try:
            html = urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "replace")
        except urllib.error.URLError:
            return False
        token = re.search(r'"SNlM0e":"([^"]+)"', html)
        if not token:
            return False
        updates = {"CWS_AT": token.group(1)}
        s.at = token.group(1)
        build = re.search(r'"cfb2h":"([^"]+)"', html)
        if build:
            s.bl = build.group(1)
            updates["CWS_BL"] = build.group(1)
        update_env(s.env_path, updates)
        return True

    # ---- data ----
    def stats(self, day_start: int = 0, day_end: Optional[int] = None) -> dict[str, dict]:
        """Fetch the full GetItemStats matrix as {qualified_name: series}. Raises CwsError if
        the session is dead."""
        if not STATS_QUERIES:
            raise CwsError("STATS_QUERIES is empty; nothing to fetch.")
        day_end = today_epoch() if day_end is None else day_end
        if day_start > day_end:
            raise CwsError("start date is after today.")
        freq = build_freq([(RPC_GET_ITEM_STATS, stats_inner(self.session.ext, g, d, day_start, day_end))
                           for g, d in STATS_QUERIES])
        status, text = self._call_with_refresh(RPC_GET_ITEM_STATS, freq)
        _require_data(status, text)
        idx_to_query = {str(3 + 2 * i): q for i, q in enumerate(STATS_QUERIES)}
        return decode_stats(text, idx_to_query)

    def weekly_users(self, day_start: int = 0, day_end: Optional[int] = None) -> dict[str, int]:
        """Convenience: just the weekly-users series as {date: count}."""
        return self.stats(day_start, day_end).get("weekly_users", {})

    def ratings(self, day_start: int = 0, day_end: Optional[int] = None) -> dict[str, list[int]]:
        """Fetch ratings as {date: [1star..5star]}."""
        day_end = today_epoch() if day_end is None else day_end
        if day_start > day_end:
            raise CwsError("start date is after today.")
        freq = build_freq([(RPC_GET_ITEM_RATINGS, ratings_inner(self.session.ext, day_start, day_end))])
        status, text = self._call_with_refresh(RPC_GET_ITEM_RATINGS, freq)
        _require_data(status, text)
        return parse_ratings(text)

    # ---- metadata ----
    def item_meta(self, ext: Optional[str] = None) -> Optional[dict[str, str]]:
        """GetItem -> {name, version, category}. Returns None if the listing can't be parsed;
        raises CwsError if the session is dead."""
        status, text = self._call_with_refresh(
            RPC_GET_ITEM, build_freq([(RPC_GET_ITEM, _compact([ext or self.session.ext]))]))
        _require_data(status, text)
        data = first_payload(text, RPC_GET_ITEM)
        try:
            listing = data[0][1] or data[0][2]  # published listing, else draft
            return {"name": listing[19][2], "version": listing[17], "category": listing[10]}
        except (TypeError, IndexError, KeyError):
            return None

    def list_items(self) -> list[str]:
        """GetPublisherItems(account) -> every extension id under the account."""
        status, text = self._call_with_refresh(
            RPC_GET_PUBLISHER_ITEMS,
            build_freq([(RPC_GET_PUBLISHER_ITEMS, _compact([self.session.account]))]))
        _require_data(status, text)
        data = first_payload(text, RPC_GET_PUBLISHER_ITEMS)
        ids: list[str] = []
        seen: set[str] = set()

        def walk(node) -> None:
            if isinstance(node, list):
                # item ids sit at record[0] in the response; stop descending once one matches
                if node and isinstance(node[0], str) and re.fullmatch(r"[a-p]{32}", node[0]):
                    if node[0] not in seen:
                        seen.add(node[0])
                        ids.append(node[0])
                    return
                for child in node:
                    walk(child)

        walk(data)
        return ids


# ============================================================================ output
def write_csvs_and_summary(series: dict[str, dict], stars: dict[str, list[int]], out_dir: str) -> None:
    """Write all CSVs to out_dir and print a console summary."""
    os.makedirs(out_dir, exist_ok=True)
    fmt = "{:,}".format
    scalars = {k: v for k, v in series.items() if "_by_" not in k}
    dims = {k: v for k, v in series.items() if "_by_" in k}

    dates = sorted(set().union(*[set(v) for v in scalars.values()])) if scalars else []
    cols = [c for c in ("weekly_users", "impressions", "pageviews", "installs", "uninstalls") if c in scalars]
    with open(os.path.join(out_dir, "acquisition_daily.csv"), "w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["date"] + cols)
        for day in dates:
            writer.writerow([day] + [scalars[c].get(day, "") for c in cols])

    for name, by_date in dims.items():
        with open(os.path.join(out_dir, f"{name}.csv"), "w", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(["date", "label", "count"])
            for day in sorted(by_date):
                for label, count in sorted(by_date[day].items(), key=lambda kv: -kv[1]):
                    writer.writerow([day, label, count])

    if stars:
        with open(os.path.join(out_dir, "ratings_daily.csv"), "w", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(["date", "1star", "2star", "3star", "4star", "5star"])
            for day in sorted(stars):
                writer.writerow([day] + stars[day])

    if scalars.get("weekly_users"):
        latest = sorted(scalars["weekly_users"].items())[-1]
        print(f"\nWEEKLY USERS: latest {fmt(latest[1])} ({latest[0]}), {len(scalars['weekly_users'])} days")
    total = lambda key: sum(scalars.get(key, {}).values())
    print(f"FUNNEL: impressions {fmt(total('impressions'))} -> pageviews {fmt(total('pageviews'))}"
          f" -> installs {fmt(total('installs'))} -> uninstalls {fmt(total('uninstalls'))}"
          f" (net {fmt(total('installs') - total('uninstalls'))})")
    if stars:
        totals = [sum(stars[d][i] for d in stars) for i in range(5)]
        n = sum(totals)
        avg = sum((i + 1) * totals[i] for i in range(5)) / n if n else 0
        print(f"RATINGS: {n} reviews, avg {avg:.2f} {totals}")
    print(f"\n{len(scalars)} scalar + {len(dims)} breakdown series:")
    for name in sorted(dims):
        by_date = dims[name]
        if not by_date:
            continue
        latest = max(by_date)
        snap = by_date[latest]
        top = ", ".join(f"{lab} {fmt(c)}" for lab, c in sorted(snap.items(), key=lambda kv: -kv[1])[:3])
        print(f"  {name:26s} (latest {latest}, total {fmt(sum(snap.values()))}): {top}")
    print(f"CSVs -> {out_dir}/")


# ============================================================================ cli
_EPOCH = date(1970, 1, 1)


def today_epoch() -> int:
    return (date.today() - _EPOCH).days


def _epoch_day(day: date) -> int:
    return (day - _EPOCH).days


def cmd_fetch(client: CwsClient, since: Optional[str], out_dir: str) -> int:
    day_start = _epoch_day(date.fromisoformat(since)) if since else 0
    day_end = today_epoch()
    series = client.stats(day_start, day_end)
    stars = client.ratings(day_start, day_end)
    meta = client.item_meta()
    print(f"{meta['name']} v{meta['version']} ({meta['category']})" if meta else f"Extension {client.session.ext}")
    print(f"  window {day_start}..{day_end} (epoch-days)")
    write_csvs_and_summary(series, stars, out_dir)
    return 0


def cmd_list(client: CwsClient) -> int:
    ids = client.list_items()
    if not ids:
        print("No extensions found on this account.")
        return 0
    print(f"{len(ids)} extension(s) under account {client.session.account}:")
    for ext in ids:
        meta = client.item_meta(ext)
        print(f"  {ext}  {meta['name'] + ' v' + meta['version'] if meta else ''}")
    return 0


def cmd_refresh_token(client: CwsClient) -> int:
    if client.refresh_token():
        print("Token + build label refreshed from the dashboard page.")
        return 0
    raise CwsError("Could not mint a token. The cookie has expired; refresh CWS_COOKIE in .env.")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cws-analytics",
        description="Pull Chrome Web Store usage analytics for your extension to CSV.",
        epilog="Configure credentials in .env (see .env.example).",
    )
    parser.add_argument("--since", metavar="YYYY-MM-DD", help="start date (default: all time)")
    parser.add_argument("--list", action="store_true", help="list every extension on the account")
    parser.add_argument("--refresh-token", action="store_true",
                        help="re-mint the XSRF token from the page and exit")
    parser.add_argument("--out", default="usage_out", metavar="DIR", help="output directory (default: usage_out)")
    parser.add_argument("--env", default=ENV_PATH, metavar="PATH", help="path to the .env file")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = parser.parse_args(argv)

    if args.since:
        try:
            since = date.fromisoformat(args.since)
        except ValueError:
            parser.error(f"--since {args.since!r} is not a valid YYYY-MM-DD date")
        if since > date.today():
            parser.error("--since is in the future")

    try:
        client = CwsClient.from_env(args.env)
        if args.refresh_token:
            return cmd_refresh_token(client)
        if args.list:
            return cmd_list(client)
        return cmd_fetch(client, args.since, args.out)
    except CwsError as exc:
        print(exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
