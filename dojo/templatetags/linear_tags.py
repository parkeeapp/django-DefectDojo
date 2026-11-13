# LINEAR-FORK: context for the Linear panel on the finding page.
from django import template

register = template.Library()


@register.inclusion_tag("dojo/linear/finding_panel.html", takes_context=True)
def linear_finding_panel(context, finding):
    from dojo.authorization.authorization import user_has_permission  # noqa: PLC0415 -- lazy import, avoids circular dependency
    from dojo.linear import services  # noqa: PLC0415 -- lazy import, avoids circular dependency
    linear = {"show": False}
    request = context.get("request")
    if finding is not None and request is not None and services.is_enabled():
        config = services.get_config(finding)
        link = services.get_link(finding)
        if config or link:
            waiting = None
            baselined = services.is_baselined(finding)
            if config and not link and not baselined and services.has_push_tag(finding, config):
                waiting = services.eligibility_problem(finding, config)
            linear = {
                "show": True,
                "config": config,
                "link": link,
                "others": link.issue.finding_links.count() - 1 if link else 0,
                "waiting": waiting,
                "baselined": baselined,
                "can_edit": user_has_permission(request.user, finding, "edit"),
            }
    return {"linear": linear, "finding": finding, "request": request, "csrf_token": context.get("csrf_token")}
