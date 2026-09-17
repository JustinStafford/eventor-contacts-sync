"""Orchestrate one run: pull from Eventor, read the contacts, plan, optionally apply.

Progress and error log lines never carry names or contact details: people are
referred to by Eventor ID and API error text is scrubbed, because in CI the log
is stored by GitHub.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

from eventor_client import EventorClient, ResponseCache

from eventor_contacts_sync.config import SyncConfig
from eventor_contacts_sync.diff import (
    ContactChange,
    Plan,
    compute_plan,
    label_names,
    memberships_for,
    plan_contact,
)
from eventor_contacts_sync.google import (
    BATCH_CREATE_MAX,
    BATCH_UPDATE_MAX,
    GoogleError,
    PeopleClient,
    make_token_provider,
)
from eventor_contacts_sync.report import build_report, render_diff, scrub
from eventor_contacts_sync.sources import PullResult, pull

log = logging.getLogger(__name__)

# Below this many affected contacts the removal guard never trips.
GUARD_MINIMUM = 10


class SafetyGuardError(Exception):
    """The plan looks like the product of a bad Eventor response; nothing was written."""


@dataclass(slots=True)
class RunResult:
    ok: bool
    apply: bool
    plan: Plan
    pull: PullResult
    report: dict[str, Any]
    diff_text: str

    @property
    def api_errors(self) -> list[ContactChange]:
        return [c for c in self.plan.changes if c.error]


def make_eventor_client(config: SyncConfig) -> EventorClient:
    cache = None
    if config.eventor_cache_dir is not None:
        cache = ResponseCache(config.eventor_cache_dir, config.eventor_cache_ttl_seconds)
    return EventorClient(
        config.eventor_base_url, config.eventor_api_key, cache=cache, min_interval=0.25
    )


def make_people_client(config: SyncConfig) -> PeopleClient:
    return PeopleClient(make_token_provider(config))


def check_guard(plan: Plan, pulled: PullResult, config: SyncConfig, labelled: int) -> None:
    """Refuse to apply a plan that strips the member label from a large share of contacts.

    ``labelled`` is how many contacts carry the member label now. An empty or
    truncated Eventor response would otherwise quietly unlabel the whole club.
    """
    if not pulled.people or pulled.stats.get("members", 0) == 0:
        raise SafetyGuardError("Eventor returned no members; refusing to write anything")
    losing = sum(1 for c in plan.changes if config.label_member in c.labels_remove)
    if losing >= GUARD_MINIMUM and losing > labelled * config.max_removal_fraction:
        raise SafetyGuardError(
            f"{losing} of {labelled} contacts would lose the '{config.label_member}' label, more "
            f"than SYNC_MAX_REMOVAL_PERCENT ({config.max_removal_fraction:.0%}). If this is "
            "expected (for example the renewal cut-off), run once with --force."
        )


def _chunks(items: list[ContactChange], size: int) -> list[list[ContactChange]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def _payload(change: ContactChange, group_ids: dict[str, str]) -> dict[str, Any]:
    assert change.body is not None
    return {**change.body, "memberships": memberships_for(change, group_ids)}


def _apply_creates(
    client: PeopleClient, creates: list[ContactChange], group_ids: dict[str, str]
) -> None:
    for chunk in _chunks(creates, BATCH_CREATE_MAX):
        try:
            created = client.batch_create([_payload(c, group_ids) for c in chunk])
        except GoogleError as exc:
            if exc.status_code is None or exc.status_code >= 500:
                # No definitive answer: the batch may have gone through. Creating the
                # contacts one by one now could duplicate all of them, so leave them
                # for the next run, which will find any that exist by Eventor ID.
                log.error(
                    "batch create gave no definitive answer (%s); not retrying", scrub(str(exc))
                )
                for change in chunk:
                    change.error = f"create outcome unknown, left for the next run: {exc}"
                continue
            log.warning("batch create rejected (%s); creating one at a time", scrub(str(exc)))
            for change in chunk:
                try:
                    client.create_contact(_payload(change, group_ids))
                    change.applied = True
                except GoogleError as single:
                    change.error = str(single)
                    log.error(
                        "failed to create Eventor #%s: %s", change.person_id, scrub(str(single))
                    )
            continue
        for change, person in zip(chunk, created, strict=False):
            change.resource_name = person.get("resourceName")
            change.applied = True


def _apply_update(
    client: PeopleClient,
    change: ContactChange,
    group_ids: dict[str, str],
    plan: Plan,
    config: SyncConfig,
) -> None:
    """Write one update; on a stale etag re-read the contact, re-plan and try once more."""
    assert change.resource_name is not None
    try:
        client.update_contact(change.resource_name, _payload(change, group_ids))
    except GoogleError as exc:
        if not exc.is_conflict or change.desired is None:
            raise
        log.info("Eventor #%s changed since it was read; re-reading", change.person_id)
        fresh = client.get_contact(change.resource_name)
        redone = plan_contact(
            change.desired,
            fresh,
            group_names={v: k for k, v in group_ids.items()},
            managed_labels=plan.managed_labels,
            config=config,
        )
        client.update_contact(change.resource_name, _payload(redone, group_ids))
    change.applied = True


def _apply_updates(
    client: PeopleClient,
    updates: list[ContactChange],
    group_ids: dict[str, str],
    plan: Plan,
    config: SyncConfig,
) -> None:
    for chunk in _chunks(updates, BATCH_UPDATE_MAX):
        try:
            client.batch_update({c.resource_name: _payload(c, group_ids) for c in chunk})  # type: ignore[misc]
        except GoogleError as exc:
            log.warning("batch update failed (%s); updating one at a time", scrub(str(exc)))
            for change in chunk:
                try:
                    _apply_update(client, change, group_ids, plan, config)
                except GoogleError as single:
                    change.error = str(single)
                    log.error(
                        "failed to update Eventor #%s: %s", change.person_id, scrub(str(single))
                    )
            continue
        for change in chunk:
            change.applied = True


def apply_plan(
    client: PeopleClient,
    plan: Plan,
    group_ids: dict[str, str],
    config: SyncConfig,
    limit: int | None = None,
) -> None:
    """Write the plan. Labels first, then creates, then updates, strictly in sequence.

    ``limit`` caps how many contacts are written, for a cautious first apply; the
    rest stay pending and are picked up by the next run.
    """
    writes = [c for c in plan.changes if c.has_writes][:limit]
    needed = sorted({name for c in writes for name in c.labels_add})
    for name in needed:
        if name not in group_ids:
            log.info("creating label %r", name)
            group_ids[name] = client.create_group(name)
    creates = [c for c in writes if c.action == "create"]
    updates = [c for c in writes if c.action in ("adopt", "update")]
    log.info("creating %d contacts, updating %d", len(creates), len(updates))
    _apply_creates(client, creates, group_ids)
    _apply_updates(client, updates, group_ids, plan, config)


def run(
    config: SyncConfig,
    *,
    apply: bool = False,
    force: bool = False,
    limit: int | None = None,
    now: datetime | None = None,
    eventor_client: EventorClient | None = None,
    people_client: PeopleClient | None = None,
    redact: bool = False,
) -> RunResult:
    """Execute a sync. Raises ``EventorError``/``GoogleError`` if a read fails.

    Write failures during ``apply`` are recorded per contact and the run
    continues; ``ok`` is ``False`` if any write failed.
    """
    started = datetime.now()
    now = now or started
    own_eventor = eventor_client is None
    own_people = people_client is None
    eventor_client = eventor_client or make_eventor_client(config)
    try:
        pulled = pull(eventor_client, config, now)
        log.info(
            "Eventor pull done in %.0fs: %d desired contacts",
            (datetime.now() - started).total_seconds(),
            len(pulled.people),
        )
        # Created after the pull: the access token lasts an hour and Eventor is slow.
        people_client = people_client or make_people_client(config)
        log.info("reading Google contacts")
        group_ids = people_client.list_groups()
        contacts = people_client.list_contacts()
        log.info(
            "%d contacts and %d labels fetched; computing the diff", len(contacts), len(group_ids)
        )
        group_names = {resource: name for name, resource in group_ids.items()}
        plan = compute_plan(
            pulled.people,
            contacts,
            group_names=group_names,
            managed_labels=pulled.managed_labels,
            config=config,
        )
        if apply:
            if not force:
                labelled = sum(
                    1 for c in contacts if config.label_member in label_names(c, group_names)
                )
                check_guard(plan, pulled, config, labelled)
            apply_plan(people_client, plan, group_ids, config, limit)
    finally:
        if own_eventor:
            eventor_client.close()
        if own_people and people_client is not None:
            people_client.close()

    ok = not any(c.error for c in plan.changes)
    report = build_report(
        plan,
        pulled,
        config,
        apply=apply,
        started_at=started,
        finished_at=datetime.now(),
        ok=ok,
        redact=redact,
    )
    diff_text = render_diff(plan, pulled, config, apply=apply, redact=redact)
    return RunResult(ok=ok, apply=apply, plan=plan, pull=pulled, report=report, diff_text=diff_text)


def with_overrides(config: SyncConfig, **overrides: Any) -> SyncConfig:
    """Return a copy of ``config`` with non-``None`` overrides applied."""
    return replace(config, **{k: v for k, v in overrides.items() if v is not None})
