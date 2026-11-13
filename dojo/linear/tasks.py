# LINEAR-FORK: background tasks. Registered via CELERY_IMPORTS. Every failure is
# logged and raised as an in-app alert; nothing propagates into a finding save.
import logging

from dojo.celery import app
from dojo.linear import services
from dojo.linear.client import LinearError
from dojo.linear.models import Linear_Issue

logger = logging.getLogger(__name__)


def alert(title, description, url=None, user=None):
    """In-app alert for the user who triggered the action (superusers when none)."""
    from dojo.models import Alerts, Dojo_User  # noqa: PLC0415 -- lazy import, avoids circular dependency
    from dojo.utils import get_current_user  # noqa: PLC0415 -- lazy import, avoids circular dependency
    user = user or get_current_user()
    recipients = [user] if user and getattr(user, "is_authenticated", False) else list(Dojo_User.objects.filter(is_superuser=True, is_active=True))
    for recipient in recipients:
        Alerts.objects.create(
            user_id=recipient,
            title=title[:250],
            description=description[:2000],
            url=url,
            source="Linear",
            icon="bullseye",
        )


def _finding_url(finding):
    try:
        return services.finding_url(finding)
    except Exception:
        return None


@app.task
def create_issue_task(finding_ids, title=None, team_id=None, *, from_tag=True, **kwargs):
    from dojo.models import Finding  # noqa: PLC0415 -- lazy import, avoids circular dependency
    from dojo.utils import get_current_user  # noqa: PLC0415 -- lazy import, avoids circular dependency
    findings = list(Finding.objects.filter(id__in=finding_ids).select_related("test__engagement__product"))
    if not findings or not services.is_enabled():
        return None
    try:
        issue, skipped = services.create_issue(findings, user=get_current_user(), title=title, team_id=team_id, from_tag=from_tag)
    except Exception as e:
        logger.exception("Linear: could not create an issue for findings %s", finding_ids)
        alert("Linear: issue not created", f"Findings {', '.join(f'#{i}' for i in finding_ids)}: {e}", _finding_url(findings[0]))
        return None
    if skipped and not from_tag:
        alert("Linear: some findings were skipped", "; ".join(f"#{f.id} ({reason})" for f, reason in skipped),
              issue.url if issue else _finding_url(findings[0]))
    return issue.identifier if issue else None


@app.task
def link_to_existing_task(finding_ids, identifier, **kwargs):
    from dojo.models import Finding  # noqa: PLC0415 -- lazy import, avoids circular dependency
    from dojo.utils import get_current_user  # noqa: PLC0415 -- lazy import, avoids circular dependency
    findings = list(Finding.objects.filter(id__in=finding_ids).select_related("test__engagement__product"))
    if not findings or not services.is_enabled():
        return None
    try:
        issue, skipped = services.link_to_existing(findings, identifier, user=get_current_user())
    except Exception as e:
        logger.exception("Linear: could not link findings %s to %s", finding_ids, identifier)
        alert(f"Linear: could not link to {identifier}", str(e), _finding_url(findings[0]))
        return None
    if skipped:
        alert("Linear: some findings were skipped", "; ".join(f"#{f.id} ({reason})" for f, reason in skipped),
              issue.url if issue else _finding_url(findings[0]))
    return issue.identifier if issue else None


@app.task
def sync_issue_status_task(issue_id, changed_finding_id=None, **kwargs):
    from dojo.models import Finding  # noqa: PLC0415 -- lazy import, avoids circular dependency
    issue = Linear_Issue.objects.filter(pk=issue_id).first()
    if not issue or not services.is_enabled():
        return
    changed = Finding.objects.filter(pk=changed_finding_id).first() if changed_finding_id else None
    try:
        services.sync_issue_status(issue, changed_finding=changed)
    except LinearError as e:
        logger.exception("Linear: status sync failed for %s", issue.identifier)
        alert(f"Linear: status sync failed for {issue.identifier}", str(e), issue.url)


@app.task
def finding_deleted_task(issue_id, removed_label, product_id, **kwargs):
    issue = Linear_Issue.objects.filter(pk=issue_id).first()
    if not issue or not services.is_enabled():
        return
    try:
        services.finding_deleted(issue, removed_label, product_id)
    except LinearError as e:
        logger.exception("Linear: delete sync failed for %s", issue.identifier)
        alert(f"Linear: status sync failed for {issue.identifier}", str(e), issue.url)
