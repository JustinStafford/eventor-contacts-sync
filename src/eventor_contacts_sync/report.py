"""Human-readable diff and machine-readable JSON report for a run."""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from eventor_contacts_sync.config import SyncConfig
from eventor_contacts_sync.diff import ContactChange, Plan
from eventor_contacts_sync.sources import PullResult

REPORT_VERSION = 1
_REDACTED = "***"


_EMAIL = re.compile(r"[^\s@<>'\"]+@[^\s@<>'\"]+")
_DIGITS = re.compile(r"\+?\d[\d\s().-]{6,}\d")


def scrub(text: str | None) -> str | None:
    """Strip anything shaped like an email address or phone number from free text."""
    if text is None:
        return None
    return _DIGITS.sub("[number]", _EMAIL.sub("[email]", text))


def _name(name: str, redact: bool) -> str:
    """Redacted output carries no part of a name; people are told apart by Eventor ID."""
    return "[redacted]" if redact and name else name


def _account(email: str, redact: bool) -> str:
    return "[redacted]@" + email.partition("@")[2] if redact else email


def _labels(labels: frozenset[str]) -> str:
    return ", ".join(sorted(labels))


def _change_detail(change: ContactChange, redact: bool) -> str:
    parts = []
    for field, (old, new) in change.field_changes.items():
        if redact or field == "sync-state":
            parts.append(field if new is not None else f"{field} removed")
        else:
            parts.append(f"{field}: {old or '(none)'} -> {new if new is not None else '(removed)'}")
    parts += [f"+{label}" for label in sorted(change.labels_add)]
    parts += [f"-{label}" for label in sorted(change.labels_remove)]
    return "; ".join(parts)


def render_diff(
    plan: Plan, pull: PullResult, config: SyncConfig, *, apply: bool, redact: bool = False
) -> str:
    """Render the plan as readable text for the terminal or a CI log."""
    lines: list[str] = []
    mode = "APPLY" if apply else "DRY RUN (no changes written; use --apply)"
    org = pull.organisation
    stats = pull.stats
    lines.append(f"Eventor -> Google Contacts sync: {mode}")
    lines.append(
        f"Organisation: {org.name or org.short_name} (#{org.id}) via {config.eventor_base_url}"
    )
    lines.append(f"Google account: {_account(config.google_user, redact)}")
    lines.append(
        f"Members: {stats.get('memberships', 0)} from /{pull.member_source} for "
        f"{', '.join(str(y) for y in pull.membership_years)}"
        + (
            f" ({stats['memberships_unpaid_skipped']} unpaid skipped)"
            if stats.get("memberships_unpaid_skipped")
            else ""
        )
    )
    lines.append(
        f"Entrants: {stats.get('entries', 0)} entries to {stats.get('events', 0)} events "
        f"between {pull.window_start:%Y-%m-%d} and {pull.window_end:%Y-%m-%d}"
    )
    lines.append(
        f"Desired contacts: {len(pull.people)} ({stats.get('with_address', 0)} with an address)   "
        f"Google contacts: {plan.contacts_total} ({plan.contacts_managed} managed)"
    )
    lines.append(f"Managed labels: {_labels(plan.managed_labels | {config.label_all})}")

    def section(title: str, changes: list[ContactChange], marker: str) -> None:
        if not changes:
            return
        lines.append("")
        lines.append(f"{title} ({len(changes)})")
        for c in changes:
            error = scrub(c.error) if redact else c.error
            status = f"  FAILED: {error}" if error else ""
            detail = _labels(c.labels_add) if c.action == "create" else _change_detail(c, redact)
            who = f"Eventor #{c.person_id}" if redact else c.name
            lines.append(f"  {marker} {who:<28} [{detail}]{status}")

    section("New contacts", plan.by_action("create"), "+")
    section("Adopted existing contacts", plan.by_action("adopt"), "@")
    section("Updates", [c for c in plan.by_action("update") if c.desired is not None], "~")
    section("Lapsed (managed labels removed, contact kept)", plan.lapsed, "-")
    section(
        f"Dependants (details moved to the owner's contact; delete these under the "
        f"'{config.label_dependant}' label if you like)",
        plan.by_action("dependant"),
        "-",
    )

    def exceptions(title: str, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        lines.append("")
        lines.append(f"{title} ({len(rows)})")
        for row in rows:
            extra = ""
            if row.get("contact_names"):
                others = ", ".join(row["contacts"] if redact else row["contact_names"])
                extra = f": {row.get('reason', 'matches')} as {others}"
            who = "" if redact else f"{row['name']} "
            lines.append(f"  ? {who}(Eventor #{row['person_id']}){extra}")

    exceptions("Possible duplicates (looks like an unmanaged contact; created anyway)",
               plan.possible_duplicates)  # fmt: skip
    exceptions("Ambiguous (several unmanaged contacts match; skipped, merge them in Google)",
               plan.ambiguous)  # fmt: skip
    exceptions("Eventor ID on more than one contact (first one used)", plan.duplicate_ids)

    if pull.dependants:
        lines.append("")
        lines.append(
            f"Dependants sharing every contact detail with someone else ({len(pull.dependants)}; "
            "no contact of their own)"
        )
        for d in pull.dependants:
            who = "" if redact else f"{d.name} "
            owner = "" if redact else f"{d.owner_name} "
            lines.append(f"  . {who}(Eventor #{d.person_id}) -> {owner}(Eventor #{d.owner_id})")

    members_missing = [p for p in pull.no_contact if p.is_member]
    if members_missing:
        lines.append("")
        lines.append(f"Members with no email or phone in Eventor ({len(members_missing)})")
        for p in members_missing:
            who = "" if redact else f"{p.name} "
            lines.append(f"  ! {who}(Eventor #{p.person_id})")
    others = len(pull.no_contact) - len(members_missing)
    if others:
        lines.append("")
        lines.append(
            f"{others} entrants from other clubs have no contact details available through "
            "the Eventor API and were not synced (listed in the report)."
        )

    s = plan.summary()
    lines.append("")
    lines.append(
        f"Summary: {s['new_contacts']} new, {s['adopted_contacts']} adopted, "
        f"{s['updated_contacts'] - s['lapsed_contacts']} updated, {s['lapsed_contacts']} lapsed, "
        + (f"{s['dependant_contacts']} dependant stubs, " if s["dependant_contacts"] else "")
        + f"{s['unchanged_contacts']} unchanged"
        + (f", {s['api_errors']} FAILED" if s["api_errors"] else "")
    )
    return "\n".join(lines)


def _change_json(change: ContactChange, redact: bool) -> dict[str, Any]:
    return {
        "person_id": change.person_id,
        "name": _name(change.name, redact),
        "action": "lapse" if change.desired is None else change.action,
        "resource_name": change.resource_name,
        "fields": {
            field: {"old": _REDACTED if redact else old, "new": _REDACTED if redact else new}
            for field, (old, new) in change.field_changes.items()
        },
        "labels_add": sorted(change.labels_add),
        "labels_remove": sorted(change.labels_remove),
        "applied": change.applied,
        "error": scrub(change.error) if redact else change.error,
    }


def build_report(
    plan: Plan,
    pull: PullResult,
    config: SyncConfig,
    *,
    apply: bool,
    started_at: datetime,
    finished_at: datetime,
    ok: bool,
    redact: bool = False,
) -> dict[str, Any]:
    def named(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {
                **row,
                "name": _name(row["name"], redact),
                **(
                    {"contact_names": [_name(n, redact) for n in row["contact_names"]]}
                    if "contact_names" in row
                    else {}
                ),
            }
            for row in rows
        ]

    return {
        "report_version": REPORT_VERSION,
        "ok": ok,
        "mode": "apply" if apply else "dry-run",
        "started_at": started_at.isoformat(timespec="seconds"),
        "finished_at": finished_at.isoformat(timespec="seconds"),
        "eventor": {
            "base_url": config.eventor_base_url,
            "organisation_id": pull.organisation.id,
            "organisation": pull.organisation.name or pull.organisation.short_name,
            "membership_years": list(pull.membership_years),
            "member_source": pull.member_source,
            "window": [pull.window_start.date().isoformat(), pull.window_end.date().isoformat()],
            "stats": pull.stats,
        },
        "google": {
            "user": _account(config.google_user, redact),
            "managed_labels": sorted(plan.managed_labels | {config.label_all}),
        },
        "summary": plan.summary(),
        "changes": [_change_json(c, redact) for c in plan.changes if c.has_writes],
        "exceptions": {
            "dependants": [
                {
                    "person_id": d.person_id,
                    "name": _name(d.name, redact),
                    "owner_id": d.owner_id,
                    "owner_name": _name(d.owner_name, redact),
                    "is_member": d.is_member,
                    "sources": list(d.sources),
                }
                for d in pull.dependants
            ],
            "no_contact_details": [
                {
                    "person_id": p.person_id,
                    "name": _name(p.name, redact),
                    "is_member": p.is_member,
                    "sources": list(p.sources),
                }
                for p in pull.no_contact
            ],
            "possible_duplicates": named(plan.possible_duplicates),
            "ambiguous_matches": named(plan.ambiguous),
            "duplicate_eventor_ids": named(plan.duplicate_ids),
            "api_errors": [_change_json(c, redact) for c in plan.changes if c.error],
        },
    }


def write_report(path: Path, report: dict[str, Any]) -> None:
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
