# Running the sync from a private repository

The public repository is the tool. Your club's Eventor key, Google identifiers, event-series
labels and run reports belong in a **private** repository: on a public repository every workflow
log and artifact is readable by anyone. The workflow installs the tool with `uvx` straight from
GitHub on every run, so there is no code to maintain here; move `TOOL_REF` when you want a newer
version.

1. Create a new private repository on GitHub, for example `yourclub-contacts-sync`. Do not fork
   the tool: forks of public repositories cannot be made private. Its `owner/name` is needed in
   the next step, because Google will trust this repository and no other.
2. Run [`scripts/setup_gcp.sh`](../../scripts/setup_gcp.sh) from the tool's repository in
   [Cloud Shell](https://shell.cloud.google.com) with `PROJECT_ID` and `GITHUB_REPO` set, then
   add the client ID it prints under *Manage Domain Wide Delegation* in the Google Admin console
   with the scope `https://www.googleapis.com/auth/contacts`. The tool's README has the detail.
3. Add [`sync.yml`](sync.yml) from this directory as `.github/workflows/sync.yml`, and optionally
   a `config.toml` with series labels (see `config.example.toml` in the tool's repository).
4. Under *Settings > Secrets and variables > Actions* add the secret `EVENTOR_API_KEY` and the
   variables `GOOGLE_USER`, `GOOGLE_SERVICE_ACCOUNT` and `GCP_WORKLOAD_IDENTITY_PROVIDER` (the
   last two are printed by the script). Set `TOOL_REF` to a tag or commit of the tool so a future
   change there cannot alter your sync until you choose to move; `main` is used when it is unset.
   Add `SYNC_LABEL_MEMBER` and `SYNC_LABEL_ENTRANT` if you want label names other than `Member`
   and `Entrant`.
5. Open *Actions*, run *Sync Google Contacts* by hand with *apply* unticked, and read the log and
   the report artifact. Run it again with *apply* ticked and *limit* set to `3`, check those
   contacts in Google Contacts, then once more with *apply* ticked and no limit.
6. Add the variable `SYNC_ENABLED` = `true`. From then on it runs nightly at 16:37 UTC
   (02:37 Sydney time in winter), editable in the `cron` line. Manual runs work either way.

Logs and the report identify people by Eventor ID only. Set the variable `SYNC_REDACT` to
`false` if you would rather see names and contact details in your private repository's logs.
