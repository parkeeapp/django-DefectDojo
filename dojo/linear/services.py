# LINEAR-FORK: business logic for the Linear integration.
#
# The `linear` tag means "this finding exists as an issue in Linear":
# - tag alone on a verified finding -> one issue for that finding (automatic);
# - "Create one Linear issue from selected" / "Link to existing Linear issue" ->
#   several findings on one issue; the tag is added after linking.
# Findings that already had the tag when Linear was enabled (the baseline) are
# never pushed: their issues were made by hand. After creation only the status is
# synced. Nothing here may break a finding save: callers dispatch tasks and every
# Linear error ends as an alert.
import logging
import re

from django.db import transaction
from django.template.loader import render_to_string
from django.urls import reverse

from dojo.linear.client import LinearClient, LinearError
from dojo.linear.models import (
    Linear_Baseline,
    Linear_Finding_Link,
    Linear_Issue,
    Linear_Product_Config,
)

logger = logging.getLogger(__name__)

LINEAR_USERNAME = "Linear"
DD_SECTION_HEADING = "**DefectDojo**"
# The DD-owned section: a horizontal rule followed by the heading, up to the end.
DD_SECTION_RE = re.compile(r"\n*^\s*(?:-{3,}|\*{3,}|_{3,})\s*\n+\s*\*\*DefectDojo\*\*.*\Z", re.MULTILINE | re.DOTALL)

STATE_OPEN = "open"
STATE_DONE = "done"
STATE_CANCELED = "canceled"

SEVERITY_RANK = {"Critical": 0, "High": 1, "Medium": 2, "Low": 3, "Info": 4}


# --- lookups -------------------------------------------------------------------

def is_enabled():
    from dojo.utils import get_system_setting  # noqa: PLC0415 -- lazy import, avoids circular dependency
    return bool(get_system_setting("enable_linear"))


def get_config(finding):
    product_id = finding.test.engagement.product_id
    return Linear_Product_Config.objects.filter(product_id=product_id, enabled=True).select_related("linear_instance").first()


def get_link(finding):
    return Linear_Finding_Link.objects.filter(finding=finding).select_related("issue").first()


def finding_tag_names(finding):
    return {t.name.lower() for t in finding.tags.all()}


def has_push_tag(finding, config):
    return bool(config.push_tag) and config.push_tag in finding_tag_names(finding)


def is_baselined(finding):
    return Linear_Baseline.objects.filter(finding=finding).exists()


def eligibility_problem(finding, config):
    """Return why a finding cannot be pushed, or None when it can."""
    if not finding.active:
        return "not active"
    if not finding.verified:
        return "not verified"
    if finding.duplicate:
        return "a duplicate"
    if SEVERITY_RANK.get(finding.severity, 4) > SEVERITY_RANK.get(config.minimum_severity, 4):
        return f"below the minimum severity ({config.minimum_severity})"
    return None


# --- the save hook -------------------------------------------------------------

def sync_if_needed(finding):
    """
    Called after every ordinary finding save (finding/helper.py post-processing).
    Cheap checks first; only dispatches a background task when there is work.
    Never raises.
    """
    try:
        if not finding or not is_enabled():
            return
        from dojo.celery_dispatch import dojo_dispatch_task  # noqa: PLC0415 -- lazy import, avoids circular dependency
        from dojo.linear import tasks  # noqa: PLC0415 -- lazy import, avoids circular dependency

        link = get_link(finding)
        if link:
            dojo_dispatch_task(tasks.sync_issue_status_task, link.issue_id, finding.id)
            return
        config = get_config(finding)
        if not config or not has_push_tag(finding, config) or is_baselined(finding):
            return
        if eligibility_problem(finding, config):
            # Tag stays; the issue is created once the finding becomes eligible.
            return
        dojo_dispatch_task(tasks.create_issue_task, [finding.id])
    except Exception:
        logger.exception("Linear: sync_if_needed failed for finding %s", getattr(finding, "id", None))


def sync_findings_if_needed(findings):
    """Bulk-edit tagging writes tags without save(); run the hook for each finding."""
    if not is_enabled():
        return
    for finding in findings:
        sync_if_needed(finding)


def on_finding_deleted(finding):
    """Called from finding_delete (pre-delete). Re-syncs the issue once the finding is gone."""
    try:
        if not is_enabled():
            return
        link = get_link(finding)
        if not link:
            return
        issue_id = link.issue_id
        label = f"#{finding.id} {finding.title}"
        product_id = finding.test.engagement.product_id
        from dojo.celery_dispatch import dojo_dispatch_task  # noqa: PLC0415 -- lazy import, avoids circular dependency
        from dojo.linear import tasks  # noqa: PLC0415 -- lazy import, avoids circular dependency
        transaction.on_commit(lambda: dojo_dispatch_task(tasks.finding_deleted_task, issue_id, label, product_id))
    except Exception:
        logger.exception("Linear: on_finding_deleted failed for finding %s", getattr(finding, "id", None))


# --- baseline ------------------------------------------------------------------

def capture_baseline(config):
    """Record every finding of the product that already carries the push tag."""
    from dojo.models import Finding  # noqa: PLC0415 -- lazy import, avoids circular dependency
    if not config.push_tag:
        return 0
    finding_ids = (
        Finding.objects.filter(test__engagement__product_id=config.product_id, tags__name=config.push_tag)
        .exclude(linear_link__isnull=False)
        .values_list("id", flat=True)
        .distinct()
    )
    rows = [Linear_Baseline(finding_id=fid) for fid in finding_ids]
    Linear_Baseline.objects.bulk_create(rows, ignore_conflicts=True)
    logger.info("Linear: baseline captured %d findings for product %s", len(rows), config.product_id)
    return len(rows)


def baseline_count_preview(product, push_tag):
    from dojo.models import Finding  # noqa: PLC0415 -- lazy import, avoids circular dependency
    if not push_tag:
        return 0
    return (
        Finding.objects.filter(test__engagement__product=product, tags__name=push_tag.strip().lower())
        .exclude(linear_link__isnull=False)
        .exclude(linear_baseline__isnull=False)
        .distinct()
        .count()
    )


# --- status --------------------------------------------------------------------

def finding_state(finding):
    if finding.active:
        return STATE_OPEN
    if finding.false_p or finding.out_of_scope or finding.risk_accepted:
        return STATE_CANCELED
    return STATE_DONE


def finding_state_reason(finding):
    if finding.active:
        return "active again"
    if finding.false_p:
        return "marked false positive"
    if finding.out_of_scope:
        return "marked out of scope"
    if finding.risk_accepted:
        return "risk accepted"
    if finding.is_mitigated:
        return "mitigated"
    return "closed"


def aggregate_state(findings):
    """Any finding open -> open; all closed with one fixed -> done; all FP/OOS/RA -> canceled."""
    states = [finding_state(f) for f in findings]
    if not states:
        return STATE_CANCELED
    if STATE_OPEN in states:
        return STATE_OPEN
    if STATE_DONE in states:
        return STATE_DONE
    return STATE_CANCELED


def state_id_for(config, state):
    return {
        STATE_OPEN: config.reopen_state_id,
        STATE_DONE: config.done_state_id,
        STATE_CANCELED: config.canceled_state_id,
    }[state]


def config_for_issue(issue):
    """The config of the first linked finding's product (team states live there)."""
    link = issue.finding_links.select_related("finding__test__engagement").order_by("id").first()
    if not link:
        return None
    return Linear_Product_Config.objects.filter(product_id=link.finding.test.engagement.product_id).select_related("linear_instance").first()


def sync_issue_status(issue, changed_finding=None, removed_label=None):
    """
    Move the Linear issue to the state its findings imply, only when it changes.
    DD only reopens an issue that DD itself closed (no webhook yet, so DD does not
    fight a state set by hand in Linear).
    """
    with transaction.atomic():
        issue = Linear_Issue.objects.select_for_update().get(pk=issue.pk)
        findings = [link.finding for link in issue.finding_links.select_related("finding")]
        config = config_for_issue(issue)
        if config is None:
            # No linked finding left (or its product lost its config): nothing to map states with.
            return
        target = aggregate_state(findings)
        target_state_id = state_id_for(config, target)
        closed_ids = {config.done_state_id, config.canceled_state_id}
        if target == STATE_OPEN and issue.last_status_pushed not in closed_ids:
            return
        if target_state_id == issue.last_status_pushed:
            return
        client = LinearClient.for_instance(issue.linear_instance)
        client.update_issue(issue.linear_id, {"stateId": target_state_id})
        issue.last_status_pushed = target_state_id
        issue.save(update_fields=["last_status_pushed"])
    if changed_finding is not None:
        reason = f"#{changed_finding.id} {changed_finding.title} was {finding_state_reason(changed_finding)} in DefectDojo."
    elif removed_label:
        reason = f"{removed_label} was deleted in DefectDojo."
    else:
        reason = "Finding status changed in DefectDojo."
    _safe_comment(client, issue, f"{reason} Issue moved to **{target}**.")


def finding_deleted(issue, removed_label, product_id):
    """A linked finding was deleted. Re-sync; when it was the last one, cancel the issue."""
    if issue.finding_links.exists():
        sync_issue_status(issue, removed_label=removed_label)
        return
    config = Linear_Product_Config.objects.filter(product_id=product_id).first()
    if not config or issue.last_status_pushed in {config.done_state_id, config.canceled_state_id}:
        return
    client = LinearClient.for_instance(issue.linear_instance)
    client.update_issue(issue.linear_id, {"stateId": config.canceled_state_id})
    issue.last_status_pushed = config.canceled_state_id
    issue.save(update_fields=["last_status_pushed"])
    _safe_comment(client, issue, f"{removed_label} was deleted in DefectDojo. No findings remain, issue moved to **canceled**.")


# --- content -------------------------------------------------------------------

def finding_url(finding):
    from dojo.utils import get_full_url  # noqa: PLC0415 -- lazy import, avoids circular dependency
    return get_full_url(reverse("view_finding", args=(finding.id,)))


# Defence in depth: Gitleaks findings carry "**Secret:** <value>" and "**Match:** <line>"
# in their description unless the scan ran with --redact. Never send a secret to Linear:
# any such line that is not already redacted is replaced.
SECRET_LINE_RE = re.compile(r"^(\*\*(?:Secret|Match):\*\*[ \t]*)(?![^\n]*REDACTED)\S.*$", re.MULTILINE)


def redact_secrets(text):
    return SECRET_LINE_RE.sub(r"\1REDACTED", text or "")


def render_content(findings):
    return redact_secrets(render_to_string("issue-trackers/linear/linear-description.tpl", {"findings": findings})).strip()


def render_dd_section(findings):
    lines = ["---", DD_SECTION_HEADING]
    for f in findings:
        product = f.test.engagement.product.name
        status = "Verified" if f.verified else ("Active" if f.active else finding_state_reason(f).capitalize())
        lines.append(f"- [#{f.id} {f.title}]({finding_url(f)}): {product} / {f.severity} / {status}")
    return "\n".join(lines)


def replace_dd_section(description, findings):
    """Rewrite only the DD-owned section at the bottom; append it when absent."""
    body = DD_SECTION_RE.sub("", description or "").rstrip()
    section = render_dd_section(findings)
    return f"{body}\n\n{section}" if body else section


def build_title(findings, title=None):
    if title:
        return title[:250]
    first = findings[0]
    if len(findings) == 1:
        return first.title[:250]
    return f"{first.title} (+{len(findings) - 1} more)"[:250]


# --- links ---------------------------------------------------------------------

def get_linear_user():
    from dojo.models import Dojo_User  # noqa: PLC0415 -- lazy import, avoids circular dependency
    user, created = Dojo_User.objects.get_or_create(username=LINEAR_USERNAME, defaults={"is_active": False})
    if created:
        user.set_unusable_password()
        user.save()
    return user


def write_dd_note(link, issue, others_count, user):
    from dojo.models import Notes  # noqa: PLC0415 -- lazy import, avoids circular dependency
    if link.dd_note_id:
        return
    how = "created by DefectDojo" if issue.created_by_dd else "linked to an existing issue"
    if others_count:
        how += f", with {others_count} other finding{'s' if others_count != 1 else ''}"
    note = Notes.objects.create(
        entry=f"Linked to Linear issue {issue.identifier} <{issue.url}> ({how}).",
        author=user or get_linear_user(),
        private=False,
    )
    # notes.add() without finding.save(): does not re-run the save hook.
    link.finding.notes.add(note)
    link.dd_note = note
    link.save(update_fields=["dd_note"])


def write_attachment(client, link, issue):
    if link.linear_attachment_id:
        return
    f = link.finding
    attachment_id = client.create_attachment(
        issue.linear_id,
        finding_url(f),
        f"DefectDojo finding #{f.id}",
        f"{f.test.engagement.product.name} / {f.severity}",
    )
    link.linear_attachment_id = attachment_id or "created"
    link.save(update_fields=["linear_attachment_id"])


def write_links(client, issue, links, user):
    """Both halves, each once. A failure here never undoes the link."""
    total = issue.finding_links.count()
    for link in links:
        try:
            write_dd_note(link, issue, total - 1, user)
        except Exception:
            logger.exception("Linear: could not write DD note for finding %s", link.finding_id)
        try:
            write_attachment(client, link, issue)
        except LinearError:
            logger.exception("Linear: could not add attachment for finding %s", link.finding_id)


def add_tag(findings, config):
    from dojo.tags.utils import bulk_add_tags_to_instances  # noqa: PLC0415 -- lazy import, avoids circular dependency
    to_tag = [f for f in findings if config.push_tag and config.push_tag not in finding_tag_names(f)]
    if to_tag:
        # Bulk helper: no save(), so the hook does not run again (they are linked anyway).
        bulk_add_tags_to_instances(tag_or_tags=config.push_tag, instances=to_tag, tag_field_name="tags")


def _safe_comment(client, issue, body):
    try:
        client.create_comment(issue.linear_id, body)
    except LinearError:
        logger.exception("Linear: could not comment on %s", issue.identifier)


# --- the two operations -------------------------------------------------------

def partition_findings(findings, *, require_unlinked=True):
    """Split into (ok, skipped[(finding, reason)]) for the buttons and the tag path."""
    ok, skipped = [], []
    for f in findings:
        config = get_config(f)
        if not config:
            skipped.append((f, "Linear is not enabled for its product"))
            continue
        if require_unlinked and get_link(f):
            skipped.append((f, "already linked to a Linear issue"))
            continue
        if problem := eligibility_problem(f, config):
            skipped.append((f, problem))
            continue
        ok.append(f)
    return ok, skipped


def create_issue(findings, user=None, title=None, team_id=None, *, from_tag=False):
    """
    Create one Linear issue for the given findings and link them all.
    Returns (issue or None, skipped). Locks the findings so two saves cannot both
    create an issue for the same finding.
    """
    from dojo.models import Finding  # noqa: PLC0415 -- lazy import, avoids circular dependency
    with transaction.atomic():
        locked = list(Finding.objects.select_for_update().filter(id__in=[f.id for f in findings]).order_by("id"))
        if from_tag:
            locked = [f for f in locked if not is_baselined(f)]
        ok, skipped = partition_findings(locked)
        if not ok:
            return None, skipped
        config = get_config(ok[0])
        instance = config.linear_instance
        other_workspace = [f for f in ok[1:] if get_config(f).linear_instance_id != instance.id]
        if other_workspace:
            skipped += [(f, "its product uses a different Linear workspace") for f in other_workspace]
            ok = [f for f in ok if f not in other_workspace]
        client = LinearClient.for_instance(instance)
        description = f"{render_content(ok)}\n\n{render_dd_section(ok)}"
        worst = min(ok, key=lambda f: SEVERITY_RANK.get(f.severity, 4))
        issue_input = {
            "teamId": team_id or config.team_id,
            "title": build_title(ok, title),
            "description": description,
            "priority": instance.priority_for(worst.severity),
        }
        if config.project_id:
            issue_input["projectId"] = config.project_id
        if config.label_ids:
            issue_input["labelIds"] = config.label_ids
        created = client.create_issue(issue_input)
        issue = Linear_Issue.objects.create(
            linear_instance=instance,
            linear_id=created["id"],
            identifier=created["identifier"],
            url=created["url"],
            team_id=issue_input["teamId"],
            created_by_dd=True,
        )
        links = [Linear_Finding_Link.objects.create(finding=f, issue=issue, linked_by=user) for f in ok]
    write_links(client, issue, links, user)
    add_tag(ok, config)
    return issue, skipped


def link_to_existing(findings, identifier, user=None):
    """Attach findings to an existing Linear issue (e.g. SEC-42). Nothing new is created."""
    from dojo.models import Finding  # noqa: PLC0415 -- lazy import, avoids circular dependency
    identifier = (identifier or "").strip()
    ok, skipped = partition_findings(findings)
    if not ok:
        return None, skipped
    config = get_config(ok[0])
    instance = config.linear_instance
    client = LinearClient.for_instance(instance)
    remote = client.get_issue(identifier)
    if not remote:
        msg = f"Linear issue {identifier} was not found in workspace {instance}"
        raise LinearError(msg)
    with transaction.atomic():
        issue, _ = Linear_Issue.objects.get_or_create(
            linear_instance=instance,
            linear_id=remote["id"],
            defaults={
                "identifier": remote["identifier"],
                "url": remote["url"],
                "team_id": (remote.get("team") or {}).get("id", ""),
                "created_by_dd": False,
            },
        )
        issue = Linear_Issue.objects.select_for_update().get(pk=issue.pk)
        locked = list(Finding.objects.select_for_update().filter(id__in=[f.id for f in ok]).order_by("id"))
        ok, more_skipped = partition_findings(locked)
        skipped += more_skipped
        links = [Linear_Finding_Link.objects.create(finding=f, issue=issue, linked_by=user) for f in ok]
        all_findings = [link.finding for link in issue.finding_links.select_related("finding__test__engagement__product").order_by("id")]
        client.update_issue(issue.linear_id, {"description": replace_dd_section(remote.get("description"), all_findings)})
    if links:
        names = ", ".join(f"#{link.finding_id}" for link in links)
        _safe_comment(client, issue, f"DefectDojo linked finding{'s' if len(links) > 1 else ''} {names} to this issue.")
    write_links(client, issue, links, user)
    add_tag(ok, config)
    sync_issue_status(issue)
    return issue, skipped


def repush_content(issue):
    """Deliberately overwrite title/description/priority from DefectDojo (confirmed in the UI)."""
    findings = [link.finding for link in issue.finding_links.select_related("finding__test__engagement__product").order_by("id")]
    if not findings:
        return
    instance = issue.linear_instance
    worst = min(findings, key=lambda f: SEVERITY_RANK.get(f.severity, 4))
    client = LinearClient.for_instance(instance)
    client.update_issue(issue.linear_id, {
        "title": build_title(findings),
        "description": f"{render_content(findings)}\n\n{render_dd_section(findings)}",
        "priority": instance.priority_for(worst.severity),
    })


def unlink(finding, user=None):
    """Remove the link and the tag; leave the Linear issue alone apart from its DD section."""
    from dojo.models import Notes  # noqa: PLC0415 -- lazy import, avoids circular dependency
    link = get_link(finding)
    if not link:
        return None
    issue = link.issue
    config = get_config(finding)
    link.delete()
    if config and config.push_tag:
        finding.tags.remove(config.push_tag)
    note = Notes.objects.create(entry=f"Unlinked from Linear issue {issue.identifier} <{issue.url}>.", author=user or get_linear_user(), private=False)
    finding.notes.add(note)
    try:
        client = LinearClient.for_instance(issue.linear_instance)
        remote = client.get_issue(issue.linear_id) or {}
        remaining = [lk.finding for lk in issue.finding_links.select_related("finding__test__engagement__product").order_by("id")]
        if remaining:
            client.update_issue(issue.linear_id, {"description": replace_dd_section(remote.get("description"), remaining)})
        _safe_comment(client, issue, f"DefectDojo unlinked finding #{finding.id} from this issue.")
    except LinearError:
        logger.exception("Linear: could not update %s after unlinking finding %s", issue.identifier, finding.id)
    return issue


def repair_links(issue, user=None):
    links = list(issue.finding_links.select_related("finding__test__engagement__product"))
    write_links(LinearClient.for_instance(issue.linear_instance), issue, links, user)
