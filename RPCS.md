# Chrome Web Store dashboard RPC reference

The developer dashboard has no public API. It talks to internal Google services through one
`batchexecute` gateway. This file lists every RPC found in the dashboard's JavaScript bundles,
so the surface is documented even though the tool calls only four of them.

This is reverse-engineered from the minified client, not from Google documentation. Treat it as
a map, not a contract. It can change when Google rebuilds the dashboard.

## How a call works

Each RPC is a `POST` to:

```
https://chrome.google.com/_/SnapcatUi/data/batchexecute?rpcids=<wire-id>
```

The body carries `f.req` (the request, a nested JSON array) and `at` (the XSRF token). The
response starts with `)]}'` and contains the payload as positional JSON arrays (JSPB), so fields
are read by position, not by name. The script in this repo handles the envelope and decoding.

`<wire-id>` is a short, build-specific id (for example `WlSRsc`). The same id is identical for
every account on a given dashboard build, but a rebuild can change it. The method names below
are stable; the wire-ids may not be. If calls start failing, re-read the ids from the page's JS
bundle, where each is registered as `new _.Mv("<id>", ..., "/<Service>.<Method>")`.

## What is verified

Four RPCs are fully decoded and used by the tool: `GetItemStats`, `GetItemRatings`, `GetItem`,
and `GetPublisherItems`. Their request shapes and response structures are confirmed against live
data. For the rest, the wire-id and method name come from the bundle, and a request shape is
noted only where a capture or a live probe confirmed it. Where no shape is given, it was not
confirmed.

The account-context reads (alerts, appeals, support issues, uploads, team, groups, owned sites)
all returned an empty payload when probed against a single-item account with no team and no open
issues. That reflects this account, not the RPC. An account that holds such data would return it,
and the populated response structure is not yet known.

The kind column (data, meta, write, config) is my classification, not Google's. It marks intent,
not a guarantee.

## SnapcatItemDataService (per extension)

| Wire id | Method | Kind | Notes |
|---|---|---|---|
| `WlSRsc` | GetItemStats | data | **Used.** Request `[[[null,[dayStart,dayEnd]],"<ext>",group,dim?]]`. group: 1 installs, 2 uninstalls, 3 impressions, 4 weekly users, 7 page views. dim: 1 os, 2 version, 3 country, 4 install source, 5 language, 6 enabled, 7-9 utm. |
| `vjWKuf` | GetItemRatings | data | **Used.** Request `[[[null,[dayStart,dayEnd]],"<ext>"]]`. Returns per-day 1-5 star counts. |
| `Mt2BP` | GetItem | meta | **Used.** Request `["<ext>"]`. Name, version, category, listing. |
| `mE4CQe` | GetPublisherItems | meta | **Used.** Request `["<account>"]`. Every item id under the account. |
| `LsTwkc` | GetItemSupportIssues | data | Request `["<ext>",null,null,null,null,25]` (last is page size). Empty for this item. |
| `AR7avc` | GetActiveAppeal | data | Request `["<ext>","__SUBMITTED"]`. Empty when no appeal is pending. |
| `Gloc4c` | GetPublishItemError | data | Request `["<ext>"]`. Last publish error, if any. |
| `y8nVDc` | GetItemTestCredentials | config | Tester login config. |
| `Z3PXjf` | GetNewCategories | config | Category taxonomy for the listing form. |
| `ezONlb` | GetSupportedLanguages | config | Language list for the listing form. |
| `csnm0` | PublishItem | write | Mutates listing state. Out of scope. |
| `IGDNge` | CancelPublishItem | write | |
| `K9P69e` | CancelSubmission | write | |
| `Z63YId` | UnpublishItem | write | |
| `RmG6ad` | ArchiveItem | write | |
| `Vddhq` | UnarchiveItem | write | |
| `qhj82d` | SaveItemDraft | write | |
| `fNXSJd` | UpdatePublishedDeployInfo | write | Staged-rollout percentage. |
| `rrqUMd` | SaveItemTestCredentials | write | |
| `AaxNP` | DeleteMedia | write | Deletes a screenshot or asset. |
| `ZtNYHd` | TransferItemToGroupPublisher | write | Moves ownership. |
| `T148Qc` | OptInToVerifiedCrxUpload | write | |
| `UDx6Zc` | OptInToGoogleAnalytics | write | Links a GA property. |
| `F9Xvab` | OptOutOfGoogleAnalytics | write | |

## SnapcatPublisherDataService (per account)

| Wire id | Method | Kind | Notes |
|---|---|---|---|
| `Kgaqm` | GetDeveloper | meta | Developer profile. Returned empty across the shapes probed. |
| `vK2CA` | GetPublisherDetails | meta | Publisher record. Empty when probed. |
| `SI1TOd` | GetRoleAndPermissions | data | Your roles and permissions. Empty when probed. |
| `K7KxXd` | GetAlerts | data | Account alerts and warnings. Empty when none. |
| `ZDP8Sb` | GetUploadAttempts | data | Package upload history. Empty when probed. |
| `gKbBfb` | GetGroupPublishers | meta | Request `[]`. Empty without a group publisher. |
| `vzA4z` | GetCreatedPublishers | meta | |
| `VNRKbf` | GetOwnedSites | meta | Request `[]`. Verified sites. Empty when none. |
| `Z3vGpd` | ListPublisherMemberships | meta | |
| `wp3ZC` | ListMemberInvitations | meta | |
| `ZO1Ut` | GetInvitation | meta | Needs an invitation id. |
| `kU2C6e` | GetCurrentUserDasherInfo | config | Enterprise identity. |
| `I7VbId` | GetPublisherForOrganizationApproval | config | Enterprise approval state. |
| `ZPTX7c` | UpdateDeveloperAlert | write | |

## NotificationsApiService

| Wire id | Method | Kind | Notes |
|---|---|---|---|
| `BLZoCe` | FetchLatestThreads | data | Notification threads. Empty when probed. |
| `fcRCpc` | FetchUserPreferences | config | Notification preferences. |
| `W5Udfe` | FetchTargetGroupPreferences | config | |

## Other endpoints

These are not batchexecute RPCs and the tool does not use them. They are recorded so the wider
surface is known.

| Endpoint | Purpose |
|---|---|
| `POST /webstore/developer/uploadv4` | Media and screenshot upload. |
| `POST /_/SnapcatUi/browserinfo` | Client telemetry ping. |
| `POST /_/SnapcatUi/web-reports` | Browser deprecation and error reports. |
| `POST accounts.google.com/RotateCookies` | Session cookie rotation. |

The OneGoogle account bar (`WidgetService.GetAccountMenuModel` and friends) and the gRPC
`OneGoogle AsyncDataService` are page chrome, unrelated to Chrome Web Store data.
