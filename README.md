# cws-analytics

Pull your Chrome Web Store extension's usage stats to CSV from the command line.

The Chrome Web Store has no public analytics API. The developer dashboard reads its numbers from an internal Google RPC endpoint. This script sends the same requests with your logged-in cookie and saves the results: weekly active users split by country, language, OS, version, enabled state, and install source; installs and uninstalls split by country, language, OS, and install source; total impressions and detail-page views; page views by UTM source, medium, and campaign; and the daily ratings breakdown.

It is unofficial. Google can change the dashboard at any time and break it. Use it only for extensions you own.

## Setup

Needs Python 3.9 or newer. No third-party packages.

Create a `.env` file next to the script with three values:

```
CWS_EXTENSION_ID=...   # 32-character id from the dashboard URL
CWS_ACCOUNT_ID=...     # publisher UUID from the dashboard URL
CWS_COOKIE=...         # your logged-in cookie for chrome.google.com
```

The extension id and account id are both in the dashboard URL when you open your item:

```
chrome.google.com/webstore/devconsole/<CWS_ACCOUNT_ID>/<CWS_EXTENSION_ID>/...
```

To get the cookie: open the dashboard while logged in, open your browser's developer tools, go to the Network tab, click any request to chrome.google.com, and copy the full `Cookie` request header. Paste it as `CWS_COOKIE`. A `.env.example` ships with the same keys.

## Install

Run it as a single file with `python cws_analytics.py`, or install it for a `cws-analytics` command. The examples below use the installed command.

```
pip install .
```

## Use

```
cws-analytics                      # all time
cws-analytics --since 2026-01-01    # from a start date
cws-analytics --list                # every extension on the account
cws-analytics --refresh-token       # mint a fresh token and exit
```

CSVs are written to `usage_out/`:

* `acquisition_daily.csv`: weekly users, impressions, page views, installs, and uninstalls per day
* `weekly_users_by_country.csv`, `installs_by_os.csv`, and the other breakdowns
* `ratings_daily.csv`: daily star counts

You can also call it from Python:

```
from cws_analytics import CwsClient
client = CwsClient.from_env()
users = client.weekly_users()   # {date: count}
series = client.stats()         # every series, keyed by name
```

## Staying logged in

You keep one thing current: the cookie. Two shorter-lived pieces are handled for you.

The XSRF token (`SNlM0e`) lasts a few minutes. The script reads a fresh one from the dashboard page whenever a call fails, so you never set it by hand.

Google rotates session cookies on responses. The script captures the rotated values and writes them to `.env`, so a session can outlast the snapshot you started with. How long it lasts is up to Google, not the script, and is not guaranteed.

Two things kill the session: logging out, or leaving the cookie unused until Google expires it. When that happens, calls return "No data" and you paste a fresh cookie.

## Limits

This reads your own dashboard and nothing else. The server checks that your account owns the extension, so the cookie only returns data for items you control.

This is unofficial and may conflict with Google's Terms of Service. How you use it is your responsibility.

The `.env` holds a live Google session cookie. Treat it like a password. It is gitignored by default. Do not commit it or share it.

## Reference

`RPCS.md` documents every RPC the dashboard bundles expose, including the ones this tool does not call.
