# eventor-contacts-sync

Keep one Google Workspace account's **Google Contacts** in sync with an orienteering club's
**Eventor** members and event entrants. Sibling of
[eventor-mailchimp-sync](https://github.com/JustinStafford/eventor-mailchimp-sync), whose Eventor
client and extraction logic it reuses.

Every night it pulls the club's current members and recent entrants from Eventor, compares them
with the account's contacts, and (only with `--apply`) creates missing contacts, updates the
details it owns, and adjusts labels. It never deletes a contact and never touches a contact it
does not manage.

```
$ eventor-contacts-sync sync
Eventor -> Google Contacts sync: DRY RUN (no changes written; use --apply)
Organisation: Example Orienteers (#123) via https://eventor.orienteering.asn.au/api
Google account: president@example.org
Members: 178 from /memberships for 2026
Entrants: 4,141 entries to 63 events between 2025-09-17 and 2026-09-17
Desired contacts: 233 (228 with an address)   Google contacts: 412 (0 managed)
Managed labels: Eventor, NOC Entrant, NOC Member, NOC Member 2026

New contacts (201)
  + New Person                   [Eventor, NOC Entrant, NOC Member, NOC Member 2026]
Adopted existing contacts (32)
  @ Sam Sample                   [mobile: (none) -> +61400000002; +Eventor; +NOC Member]
...
Summary: 201 new, 32 adopted, 0 updated, 0 lapsed, 0 unchanged
```

## How it works

Eventor has no webhooks, so each run is a **full pull and an idempotent diff**: no cursor, no
database, nothing to get out of step. Run it twice and the second run does nothing.

1. `GET /organisation/apiKey` identifies the club.
2. `GET /persons/organisations/{id}` fetches everyone attached to the club with contact details.
   This is the only endpoint that returns **postal addresses**, and it covers lapsed members too.
3. `GET /memberships` for the current year (and last year's until the end of March) says who is
   a paid member.
4. `GET /events` for a trailing window (default 12 months), then `GET /entries` for those events,
   says who entered.
5. One desired contact is built **per Eventor person** who is a current member or an entrant *and*
   has an email or phone number. `/entries` carries no contact details, so entrants from other
   clubs can only be counted and listed in the report.
6. The account's contacts and labels are read through the
   [People API](https://developers.google.com/people/v1/contacts), the diff is printed and written
   to `report.json`, and with `--apply` the changes are written in batches of up to 200, strictly
   one request at a time as Google asks.

### What is written to a contact

| Field | Source | Notes |
| --- | --- | --- |
| Name | Eventor given and family name | `SYNC_UPDATE_NAMES=false` to leave names alone |
| Email | membership email, else person email | lower-cased |
| Mobile, other phone | Eventor mobile and phone | E.164 (`0412 345 678` becomes `+61412345678`) |
| Address | `/persons/organisations` | street, suburb, state, postcode, country. The Australian instance stores the state in the `careOf` attribute. `SYNC_ADDRESSES=false` to skip |
| Labels | see below | |
| `externalIds` type `eventor` | Eventor person ID | the matching key; invisible in the Contacts UI |
| `clientData` `eventor:*` | the values last written | lets the sync replace *its* entry only |

Labels: `Eventor` on everyone the sync manages (never removed), `NOC Member`, `NOC Member YYYY`
per synced year, `NOC Entrant`, plus any series labels from `config.toml`. The last four are
**managed labels**: they are removed when the person no longer qualifies. Membership lapse is a
label removal, never a delete. Old `NOC Member YYYY` labels for years no longer synced are left
as history.

### The rules that keep your address book safe

- **Matching is by Eventor person ID**, stored on the contact. A changed name or email in Eventor
  is an update, never a duplicate. A family sharing one email address is one contact each.
- **Adoption.** A contact you made by hand is adopted (stamped with the Eventor ID and from then
  on managed) only if its name matches *and* it shares an email or phone number with the Eventor
  person. Same name but no shared detail: a new contact is created and the pair is listed under
  *possible duplicates* for you to merge in Google Contacts (a merged contact keeps the ID). Two
  hand-made contacts that both match: nothing is written and it is listed as *ambiguous*.
  `SYNC_ADOPT_EXISTING=false` turns adoption off.
- **Field ownership.** The sync only replaces the email, phone or address entry it wrote last
  time; if Eventor's value is already on the contact nothing changes; otherwise it is added
  alongside. Your own extra numbers, addresses, notes, photos, birthdays and labels are never
  modified, and a value missing from Eventor never blanks anything.
- **No deletes.** The People API client has no delete method. Creates are never retried blindly
  (a timed-out create may have succeeded; the next run finds it by ID).
- **Safety guard.** `--apply` refuses to run (exit code 4) if Eventor returned no members, or if
  more than `SYNC_MAX_REMOVAL_PERCENT` (default 25%) of contacts labelled `NOC Member` would lose
  that label. The one time this is expected is the renewal cut-off at the start of April: run the
  workflow by hand once with *force* ticked.
- Stale etags (you edited a contact while the sync was running) are re-read and retried once.

## Setup

You need: an Eventor API key, super-admin access to the Google Workspace domain, and admin access
to this GitHub repository.

### 1. Google Cloud (about five minutes, no keys created)

Open [Cloud Shell](https://shell.cloud.google.com) signed in as a Workspace admin, upload
[`scripts/setup_gcp.sh`](scripts/setup_gcp.sh) (or paste it), and run:

```bash
PROJECT_ID=noc-contacts-sync GITHUB_REPO=JustinStafford/eventor-contacts-sync bash setup_gcp.sh
```

Project IDs are globally unique; pick another if that one is taken. Add
`LOCAL_USER=you@yourdomain` if you also want to run the sync from your own machine. The script
enables the People and IAM Credentials APIs, creates a service account, and sets up Workload
Identity Federation so that **only this repository's workflows** can ask that service account to
sign on its behalf. It prints the values for the next two steps.

### 2. Google Admin console (domain-wide delegation)

[admin.google.com](https://admin.google.com) > *Security* > *Access and data control* >
*API controls* > *Manage Domain Wide Delegation* > *Add new*:

- **Client ID**: the number the script printed
- **OAuth scopes**: `https://www.googleapis.com/auth/contacts`

This is what allows the service account to act as `GOOGLE_USER`, and only for contacts. It can
take a few minutes (occasionally up to a day) to take effect.

### 3. GitHub repository settings

*Settings* > *Secrets and variables* > *Actions*:

| Kind | Name | Value |
| --- | --- | --- |
| Secret | `EVENTOR_API_KEY` | the club's Eventor API key |
| Variable | `GOOGLE_USER` | the account whose contacts are synced, e.g. `president@yourdomain` |
| Variable | `GOOGLE_SERVICE_ACCOUNT` | printed by the script |
| Variable | `GCP_WORKLOAD_IDENTITY_PROVIDER` | printed by the script |

### 4. Dry run, then switch it on

*Actions* > *Sync Google Contacts* > *Run workflow* with *apply* unticked. Read the log and the
`sync-report` artifact, in particular *Adopted existing contacts* and *Possible duplicates*. When
it looks right, run it again with *apply* ticked and *limit* set to `3`, check those three
contacts in Google Contacts, then run it once more with *apply* ticked and no limit, and then add the variable
`SYNC_ENABLED` = `true` to turn on the nightly run (16:37 UTC, 02:37 Sydney time in winter).

GitHub switches schedules off in repositories with no commits for 60 days; it emails you first.

### Running locally

```bash
uv sync
cp .env.example .env   # fill it in
uv run eventor-contacts-sync whoami
uv run eventor-contacts-sync sync
```

Keyless local runs need the [gcloud CLI](https://cloud.google.com/sdk/docs/install),
`gcloud auth application-default login`, and `LOCAL_USER` passed to the setup script. Without
gcloud, use the workflow's dry run instead.

`sync` options: `--apply`, `--limit N`, `--force`, `--report PATH`, `--config PATH`, `--window-months N`,
`--redact` (no names or contact details in the diff, report or errors; Eventor IDs only), `--quiet`, and `--no-progress`
before the command.

Exit codes: `0` success, `1` configuration problem, `2` Eventor API failure, `3` Google API
failure (including any write that failed during `--apply`; the other writes still go through),
`4` safety guard refused.

## Configuration reference

All environment variables; `.env` is loaded automatically. An empty value means "use the default".

| Variable | Default | Meaning |
| --- | --- | --- |
| `EVENTOR_API_KEY` | | Club API key |
| `GOOGLE_USER` | | Workspace account whose contacts are synced |
| `GOOGLE_SERVICE_ACCOUNT` | | Service account with domain-wide delegation (keyless auth) |
| `GOOGLE_SERVICE_ACCOUNT_FILE` | unset | Key file alternative to keyless auth |
| `EVENTOR_BASE_URL` | Australian instance | Eventor instance, with `/api` |
| `EVENTOR_CACHE_DIR` | unset | Cache Eventor responses between dry runs |
| `SYNC_WINDOW_MONTHS` | `12` | Trailing window for events and entrants |
| `SYNC_PREVIOUS_YEAR_UNTIL_MONTH` | `3` | Last year's members still count up to this month (`0` disables) |
| `SYNC_REQUIRE_PAID` | `true` | Ignore unpaid memberships |
| `SYNC_LABEL_ALL` | `Eventor` | Permanent label on every managed contact |
| `SYNC_LABEL_MEMBER` | `NOC Member` | Member label; the year label is this plus the year |
| `SYNC_LABEL_ENTRANT` | `NOC Entrant` | Entrant label |
| `SYNC_UPDATE_NAMES` | `true` | Update names from Eventor |
| `SYNC_ADDRESSES` | `true` | Sync postal addresses |
| `SYNC_ADOPT_EXISTING` | `true` | Adopt matching hand-made contacts |
| `SYNC_MAX_REMOVAL_PERCENT` | `25` | Safety guard threshold |
| `SYNC_PHONE_COUNTRY_CODE` | by instance (`61`) | For E.164 formatting; `none` to leave numbers as typed |
| `SYNC_REPORT_PATH` | `report.json` | JSON report location |

Series labels: copy [`config.example.toml`](config.example.toml) to `config.toml` and commit it.

## The run report

```jsonc
{
  "ok": true, "mode": "dry-run",
  "eventor": { "organisation_id": 123, "membership_years": [2026], "stats": {...} },
  "google": { "user": "president@example.org", "managed_labels": [...] },
  "summary": { "new_contacts": 4, "adopted_contacts": 1, "updated_contacts": 12, "lapsed_contacts": 2, ... },
  "changes": [ { "person_id": 1001, "name": "...", "action": "create|adopt|update|lapse",
                 "fields": { "email": { "old": "...", "new": "..." } },
                 "labels_add": [...], "labels_remove": [...], "applied": true, "error": null } ],
  "exceptions": {
    "no_contact_details": [...],     // members without email/phone, and entrants from other clubs
    "possible_duplicates": [...],    // created, but an unmanaged contact has the same name
    "ambiguous_matches": [...],      // skipped: several unmanaged contacts match
    "duplicate_eventor_ids": [...],  // the same Eventor ID on two contacts
    "api_errors": [...]              // failed writes; these make the run exit non-zero
  }
}
```

**Personal data stays in Eventor and Google.** The workflow always runs with `--redact`: the log
and the report artifact identify people by Eventor ID only, never by name, email, phone or
address; field changes are recorded as the field name alone; and API error text is scrubbed of
anything shaped like an email address or phone number. Progress log lines never carry names in
any mode. To see names, run the sync locally without `--redact`; `report.json` is git-ignored.

## Development

```bash
uv sync
uv run pytest
uv run ruff check . && uv run ruff format --check .
```

No test touches the network: HTTP is mocked with `respx`, the People API is faked in memory
(`tests/helpers.py`), and a guard in `tests/conftest.py` refuses real socket connections.

The Eventor client comes from `eventor-mailchimp-sync`, pinned to a commit in `pyproject.toml`
(`[tool.uv.sources]`); move the pin and run `uv lock` to pick up client changes.
