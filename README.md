# eventor-contacts-sync

Keep one Google Workspace account's **Google Contacts** in sync with an orienteering club's
**Eventor** members and event entrants. Built for clubs on the Australian Eventor instance.
Sibling of [eventor-mailchimp-sync](https://github.com/JustinStafford/eventor-mailchimp-sync),
which does the same for a Mailchimp audience.

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
Managed labels: Entrant, Eventor, Member, Member 2026

New contacts (201)
  + New Person                   [Entrant, Eventor, Member, Member 2026]
Adopted existing contacts (32)
  @ Sam Sample                   [mobile: (none) -> +61400000002; +Eventor; +Member]
...
Summary: 201 new, 32 adopted, 0 updated, 0 lapsed, 0 unchanged
```

## Contents

- [How it works](#how-it-works)
- [What is written to a contact](#what-is-written-to-a-contact)
- [The rules that keep your address book safe](#the-rules-that-keep-your-address-book-safe)
- [Setting it up for your club](#setting-it-up-for-your-club)
- [Running it locally](#running-it-locally)
- [Configuration reference](#configuration-reference)
- [The run report](#the-run-report)
- [Development](#development)
- [Licence](#licence)

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
   clubs can only be counted and listed in the report. Each email address and phone number then
   goes on **one** contact only (see *one owner per detail* below).
6. The account's contacts and labels are read through the
   [People API](https://developers.google.com/people/v1/contacts), the diff is printed and written
   to `report.json`, and with `--apply` the changes are written in batches of up to 200, strictly
   one request at a time as Google asks.

## What is written to a contact

| Field | Source | Notes |
| --- | --- | --- |
| Name | Eventor given and family name | `SYNC_UPDATE_NAMES=false` to leave names alone |
| Email | membership email, else person email | lower-cased |
| Mobile, other phone | Eventor mobile and phone | E.164 (`0412 345 678` becomes `+61412345678`) |
| Address | `/persons/organisations` | street, suburb, state, postcode, country. The Australian instance stores the state in the `careOf` attribute. `SYNC_ADDRESSES=false` to skip |
| Labels | see below | |
| `externalIds` type `eventor` | Eventor person ID | the matching key; invisible in the Contacts UI |
| `clientData` `eventor:*` | the values last written | lets the sync replace *its* entry only |

Labels: `Eventor` on everyone the sync manages (never removed), `Member`, `Member YYYY` per
synced year, `Entrant`, `Dependant`, plus any series labels from `config.toml`. All but the
first are **managed labels**: they are removed when the person no longer qualifies. Membership lapse is a label
removal, never a delete. Old `Member YYYY` labels for years no longer synced are left as history.
The label names are configurable (`SYNC_LABEL_MEMBER`, `SYNC_LABEL_ENTRANT`), for example
`Club Member` and `Club Entrant`; the sync finds labels by name, so renaming a label in Google
Contacts and changing the variable together keeps every membership.

## The rules that keep your address book safe

- **Matching is by Eventor person ID**, stored on the contact. A changed name or email in Eventor
  is an update, never a duplicate.
- **One owner per detail.** Parents routinely register children with their own email and mobile.
  A number that sits on two cards makes a phone's messaging app pick one of them at random, so
  every shared email address or phone number is given to one person: a current member before a
  non-member, then the oldest by birth date, then the lowest Eventor ID (the rule the Mailchimp
  sibling uses). The others have that value *withheld*: the entry the sync wrote is removed from
  their contact, anything you added by hand stays. Someone left with no detail of their own is a
  **dependant**: no contact is made for them and they are listed in the report against the
  owner. If a contact already exists for a dependant, it is stripped of the sync-written details,
  the managed labels come off and the `Dependant` label goes on (`SYNC_LABEL_DEPENDANT`), so you
  can open that label in Google Contacts and delete the stubs in one go. A dependant who later
  gets their own mobile becomes a contact again.
- **Adoption.** A contact you made by hand is adopted (stamped with the Eventor ID and from then
  on managed) only if its name matches *and* it shares an email or phone number with the Eventor
  person. Same name but no shared detail: a new contact is created and the pair is listed under
  *possible duplicates* for you to merge in Google Contacts (a merged contact keeps the ID). Two
  hand-made contacts that both match: nothing is written and it is listed as *ambiguous*.
  `SYNC_ADOPT_EXISTING=false` turns adoption off.
- **Field ownership.** The sync only replaces the email, phone or address entry it wrote last
  time; if Eventor's value is already on the contact nothing changes; otherwise it is added
  alongside. Your own extra numbers, addresses, notes, photos, birthdays and labels are never
  modified, and a value missing from Eventor never blanks anything (only a value *withheld*
  because it belongs to someone else is withdrawn, and only the sync's own entry).
- **No deletes.** The People API client has no delete method. Creates are never retried blindly
  (a timed-out create may have succeeded; the next run finds it by ID).
- **Safety guard.** `--apply` refuses to run (exit code 4) if Eventor returned no members, or if
  more than `SYNC_MAX_REMOVAL_PERCENT` (default 25%) of contacts carrying the member label would
  lose it. The one time this is expected is the renewal cut-off at the start of April: run the
  workflow by hand once with *force* ticked.
- Stale etags (you edited a contact while the sync was running) are re-read and retried once.
- **Personal data stays in Eventor and Google.** The suggested workflow runs with `--redact`:
  the log and the report artifact identify people by Eventor ID only, never by name, email,
  phone or address; field changes are recorded as the field name alone; and API error text is
  scrubbed of anything shaped like an email address or phone number. Progress log lines never
  carry names in any mode.

## Setting it up for your club

The suggested setup is a small **private** GitHub repository that runs the sync every night and
installs this tool from GitHub each time. You do not need to clone this repository. You need: an
Eventor API key, super-admin access to the Google Workspace domain, and a GitHub account.
Nothing is billed: the sync uses the People API within its free quota, and no Google key is ever
created.

1. **Get an Eventor API key.** Club API keys are issued by your federation (in Australia, ask
   Orienteering Australia). The key identifies your club; the tool discovers the club ID itself.
2. **Create the private runner repository.** Make a new private repository on GitHub, for
   example `yourclub-contacts-sync` (do not fork this one; forks of public repositories cannot be
   made private). Google will be told to trust this repository and no other, so its `owner/name`
   is needed in the next step.
3. **Google Cloud (about five minutes, no keys created).** Open
   [Cloud Shell](https://shell.cloud.google.com) signed in as a Workspace admin, upload
   [`scripts/setup_gcp.sh`](scripts/setup_gcp.sh) (or paste it), and run:

   ```bash
   PROJECT_ID=yourclub-contacts-sync GITHUB_REPO=you/yourclub-contacts-sync bash setup_gcp.sh
   ```

   Project IDs are globally unique; pick another if that one is taken. Add
   `LOCAL_USER=you@yourdomain` if you also want to run the sync from your own machine. The script
   enables the People and IAM Credentials APIs, creates a service account, and sets up Workload
   Identity Federation so that **only your runner repository's workflows** can ask that service
   account to sign on its behalf. It prints the values for the next two steps.
4. **Google Admin console (domain-wide delegation).** [admin.google.com](https://admin.google.com)
   > *Security* > *Access and data control* > *API controls* > *Manage Domain Wide Delegation* >
   *Add new*:

   - **Client ID**: the number the script printed
   - **OAuth scopes**: `https://www.googleapis.com/auth/contacts`

   This is what allows the service account to act as `GOOGLE_USER`, and only for contacts. It can
   take a few minutes (occasionally up to a day) to take effect.
5. **Fill in the runner repository.**
   - copy [`examples/private-runner/sync.yml`](examples/private-runner/sync.yml) to
     `.github/workflows/sync.yml`;
   - under *Settings > Secrets and variables > Actions*, add the secret `EVENTOR_API_KEY` and the
     variables `GOOGLE_USER` (the account whose contacts are synced, e.g.
     `president@yourdomain`), `GOOGLE_SERVICE_ACCOUNT` and `GCP_WORKLOAD_IDENTITY_PROVIDER`
     (both printed by the script), and `TOOL_REF` = a tag or commit of this repository, so the
     sync only changes when you move the pin.

   [`examples/private-runner/README.md`](examples/private-runner/README.md) has the same steps
   in more detail.
6. **Dry run, then switch it on.** *Actions* > *Sync Google Contacts* > *Run workflow* with
   *apply* unticked. Read the log and the `sync-report` artifact, in particular *Adopted
   existing contacts* and *Possible duplicates*. When it looks right, run it again with *apply*
   ticked and *limit* set to `3`, check those three contacts in Google Contacts, then run it once
   more with *apply* ticked and no limit. Finally add the variable `SYNC_ENABLED` = `true` to
   turn on the nightly run (16:37 UTC, 02:37 Sydney time in winter; GitHub cron is UTC only, so
   edit the `cron` line to change it).
7. **Optionally label entrants by event series.** Add a `config.toml` to the runner repository
   (see [`config.example.toml`](config.example.toml)), for example `Sprint Series` for everyone
   who entered a Sprint Series round. The workflow picks it up automatically; locally, keep it
   next to `.env`.

Why private: GitHub keeps secrets safe on any repository, but on a public one every workflow log
and artifact is readable by anyone. The suggested workflow also redacts names and contact details
from logs and the report (set the variable `SYNC_REDACT` to `false` to see them in your private
repository). GitHub switches schedules off in repositories with no commits for 60 days; it emails
you first.

This repository's own `.github/workflows/sync.yml` is the same workflow gated on a repository
variable `SYNC_ENABLED`, so it stays dormant here. `.github/workflows/ci.yml` runs ruff and
pytest on every push and pull request.

## Running it locally

Local runs need [uv](https://docs.astral.sh/uv/) and, for keyless Google access, the
[gcloud CLI](https://cloud.google.com/sdk/docs/install) with
`gcloud auth application-default login` as the `LOCAL_USER` you passed to the setup script.
Without gcloud, use the workflow's dry run instead, or point `GOOGLE_SERVICE_ACCOUNT_FILE` at a
service-account key file if your organisation allows keys.

In an empty folder, create a `.env` (see [`.env.example`](.env.example) for every option):

```
EVENTOR_API_KEY=...
GOOGLE_USER=president@yourdomain
GOOGLE_SERVICE_ACCOUNT=eventor-contacts-sync@yourclub-contacts-sync.iam.gserviceaccount.com
```

Then check both connections and do a dry run (the default; nothing is written):

```bash
uvx --from git+https://github.com/JustinStafford/eventor-contacts-sync eventor-contacts-sync whoami
```

```bash
uvx --from git+https://github.com/JustinStafford/eventor-contacts-sync eventor-contacts-sync sync
```

The dry run takes a few minutes (Eventor is slow to assemble a year of entries) and prints
progress as it goes. Without `--redact` the diff and `report.json` carry names and contact
details, so keep them on your own machine. From a clone, `uv sync` then
`uv run eventor-contacts-sync sync` does the same.

`sync` options: `--apply`, `--limit N`, `--force`, `--report PATH`, `--config PATH`,
`--window-months N`, `--redact` (no names or contact details in the diff, report or errors;
Eventor IDs only) and `--quiet`; `--no-progress` goes before the command.

Exit codes: `0` success, `1` configuration problem, `2` Eventor API failure, `3` Google API
failure (including any write that failed during `--apply`; the other writes still go through),
`4` safety guard refused.

## Configuration reference

All environment variables; `.env` is loaded automatically. An empty value means "use the default"
(GitHub Actions passes unset variables as empty strings).

| Variable | Default | Meaning |
| --- | --- | --- |
| `EVENTOR_API_KEY` | | Club API key |
| `GOOGLE_USER` | | Workspace account whose contacts are synced |
| `GOOGLE_SERVICE_ACCOUNT` | | Service account with domain-wide delegation (keyless auth) |
| `GOOGLE_SERVICE_ACCOUNT_FILE` | unset | Key file alternative to keyless auth |
| `EVENTOR_BASE_URL` | Australian instance | Eventor instance, with `/api` |
| `EVENTOR_CACHE_DIR` | unset | Cache Eventor responses between dry runs |
| `EVENTOR_CACHE_TTL_SECONDS` | `3600` | Cache lifetime |
| `SYNC_WINDOW_MONTHS` | `12` | Trailing window for events and entrants |
| `SYNC_PREVIOUS_YEAR_UNTIL_MONTH` | `3` | Last year's members still count up to this month (`0` disables) |
| `SYNC_REQUIRE_PAID` | `true` | Ignore unpaid memberships |
| `SYNC_LABEL_ALL` | `Eventor` | Permanent label on every managed contact |
| `SYNC_LABEL_MEMBER` | `Member` | Member label; the year label is this plus the year |
| `SYNC_LABEL_ENTRANT` | `Entrant` | Entrant label |
| `SYNC_LABEL_DEPENDANT` | `Dependant` | Label on the stub contact of someone whose every detail belongs to another person |
| `SYNC_UPDATE_NAMES` | `true` | Update names from Eventor |
| `SYNC_ADDRESSES` | `true` | Sync postal addresses |
| `SYNC_ADOPT_EXISTING` | `true` | Adopt matching hand-made contacts |
| `SYNC_MAX_REMOVAL_PERCENT` | `25` | Safety guard threshold |
| `SYNC_PHONE_COUNTRY_CODE` | by instance (`61`) | For E.164 formatting; `none` to leave numbers as typed |
| `SYNC_CONFIG` | `config.toml` if present | TOML file with series labels |
| `SYNC_REPORT_PATH` | `report.json` | JSON report location |

`config.toml`:

```toml
[series.sprint]
label = "Sprint Series"
event_ids = [12345, 12346]              # match by Eventor event ID (parents of multi-day events too)
name_patterns = ["sprint series"]       # and/or case-insensitive regular expressions on the event name
```

## The run report

```jsonc
{
  "ok": true, "mode": "dry-run",
  "eventor": { "organisation_id": 123, "membership_years": [2026], "stats": {...} },
  "google": { "user": "president@example.org", "managed_labels": [...] },
  "summary": { "new_contacts": 4, "adopted_contacts": 1, "updated_contacts": 12, "lapsed_contacts": 2,
               "dependant_contacts": 1, ... },
  "changes": [ { "person_id": 1001, "name": "...", "action": "create|adopt|update|lapse|dependant",
                 "fields": { "email": { "old": "...", "new": "..." } },
                 "labels_add": [...], "labels_remove": [...], "applied": true, "error": null } ],
  "exceptions": {
    "dependants": [...],             // every detail belongs to another person (owner_id, owner_name)
    "no_contact_details": [...],     // members without email/phone, and entrants from other clubs
    "possible_duplicates": [...],    // created, but an unmanaged contact has the same name
    "ambiguous_matches": [...],      // skipped: several unmanaged contacts match
    "duplicate_eventor_ids": [...],  // the same Eventor ID on two contacts
    "api_errors": [...]              // failed writes; these make the run exit non-zero
  }
}
```

With `--redact`, `name` and the old and new field values read `[redacted]` and people are
identified by `person_id` only.

## Development

```bash
uv sync
uv run pytest
uv run ruff check . && uv run ruff format --check .
```

No test touches the network: HTTP is mocked with `respx`, the People API is faked in memory
(`tests/helpers.py`), and a guard in `tests/conftest.py` refuses real socket connections.

`src/eventor_client` is a verbatim copy of the Eventor API client in
[eventor-mailchimp-sync](https://github.com/JustinStafford/eventor-mailchimp-sync), which is its
home and documents it. Keep the two identical: carry a fix across by copying the directory and
`tests/test_client.py` and `tests/test_parsing.py`, then run the tests.

## Licence

[MIT](LICENSE). Not affiliated with Eventor, Orienteering Australia or Google.
