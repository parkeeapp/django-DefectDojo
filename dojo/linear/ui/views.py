# LINEAR-FORK: thin views; logic lives in dojo/linear/services.py.
# Permissions are checked here (these URLs are not in authorization/url_permissions.py).
import logging

from django.contrib import messages
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from dojo.authorization.authorization import user_has_configuration_permission_or_403, user_has_permission_or_403
from dojo.linear import services
from dojo.linear.client import LinearClient, LinearError
from dojo.linear.models import Linear_Instance, Linear_Product_Config
from dojo.linear.ui.forms import DeleteLinearInstanceForm, LinearInstanceForm, LinearProductConfigForm
from dojo.models import Finding, Product
from dojo.utils import add_breadcrumb, dojo_crypto_encrypt

logger = logging.getLogger(__name__)


# --- Linear connections (configuration permission) ------------------------------

def linear_instances(request):
    user_has_configuration_permission_or_403(request.user, "dojo.view_linear_instance")
    add_breadcrumb(title=_("Linear"), top_level=True, request=request)
    return render(request, "dojo/linear/instances.html", {"instances": Linear_Instance.objects.all().order_by("name")})


def edit_linear_instance(request, lid=None):
    instance = get_object_or_404(Linear_Instance, pk=lid) if lid else Linear_Instance()
    user_has_configuration_permission_or_403(request.user, "dojo.change_linear_instance" if lid else "dojo.add_linear_instance")
    form = LinearInstanceForm(request.POST or None, instance=instance)
    if request.method == "POST" and form.is_valid():
        new_key = form.cleaned_data.get("api_key")
        obj = form.save(commit=False)
        try:
            client = LinearClient(new_key, obj.auth_type) if new_key else LinearClient.for_instance(obj)
            info = client.viewer()
        except LinearError as e:
            messages.add_message(request, messages.ERROR, _("Linear connection test failed: %(e)s") % {"e": e}, extra_tags="alert-danger")
        else:
            if new_key:
                obj.api_key = dojo_crypto_encrypt(new_key)
            obj.workspace_name = info.get("workspace") or ""
            obj.save()
            messages.add_message(request, messages.SUCCESS, _("Linear connection saved (workspace: %(w)s).") % {"w": obj.workspace_name}, extra_tags="alert-success")
            return HttpResponseRedirect(reverse("linear"))
    add_breadcrumb(title=_("Edit Linear connection") if lid else _("Add Linear connection"), top_level=False, request=request)
    return render(request, "dojo/linear/edit_instance.html", {"form": form, "instance": instance})


def delete_linear_instance(request, lid):
    instance = get_object_or_404(Linear_Instance, pk=lid)
    user_has_configuration_permission_or_403(request.user, "dojo.delete_linear_instance")
    form = DeleteLinearInstanceForm(request.POST or None, instance=instance)
    if request.method == "POST" and str(instance.id) == request.POST.get("id"):
        if Linear_Product_Config.objects.filter(linear_instance=instance).exists() or instance.linear_issue_set.exists():
            messages.add_message(request, messages.ERROR, _("This connection is still used by products or linked issues."), extra_tags="alert-danger")
        else:
            instance.delete()
            messages.add_message(request, messages.SUCCESS, _("Linear connection deleted."), extra_tags="alert-success")
        return HttpResponseRedirect(reverse("linear"))
    add_breadcrumb(title=_("Delete"), top_level=False, request=request)
    return render(request, "dojo/linear/delete_instance.html", {"instance": instance, "form": form})


# --- per-product config (product edit permission) ------------------------------

def _linear_options(instance_id, team_id):
    """Load teams / states / labels from Linear for the dropdowns. Returns (teams, states, labels, error)."""
    if not instance_id:
        return None, None, None, None
    try:
        client = LinearClient.for_instance(Linear_Instance.objects.get(pk=instance_id))
        teams = client.teams()
        states = client.workflow_states(team_id) if team_id else None
        labels = client.labels(team_id) if team_id else None
    except (LinearError, Linear_Instance.DoesNotExist, KeyError, TypeError) as e:
        return None, None, None, str(e)
    return teams, states, labels, None


def product_linear_config(request, pid):
    product = get_object_or_404(Product, pk=pid)
    user_has_permission_or_403(request.user, product, "edit")
    config = Linear_Product_Config.objects.filter(product=product).first() or Linear_Product_Config(product=product)
    data = request.POST or None
    instance_id = (data or {}).get("linear_instance") or config.linear_instance_id or (Linear_Instance.objects.values_list("id", flat=True).first())
    team_id = (data or {}).get("team_id") or config.team_id
    teams, states, labels, error = _linear_options(instance_id, team_id)
    if error:
        messages.add_message(request, messages.WARNING, _("Could not load options from Linear: %(e)s") % {"e": error}, extra_tags="alert-warning")
    initial = {} if config.pk else {"linear_instance": instance_id}
    form = LinearProductConfigForm(data, instance=config, initial=initial, teams=teams, states=states, labels=labels)
    if data is not None and "refresh" not in data and form.is_valid():
        obj = form.save(commit=False)
        obj.product = product
        obj.team_name = next((t["name"] for t in (teams or []) if t["id"] == obj.team_id), obj.team_name)
        obj.save()
        messages.add_message(request, messages.SUCCESS, _("Linear settings saved."), extra_tags="alert-success")
        return HttpResponseRedirect(reverse("product_linear_config", args=(product.id,)))
    push_tag = (data or {}).get("push_tag") or config.push_tag
    add_breadcrumb(parent=product, title=_("Linear"), top_level=False, request=request)
    return render(request, "dojo/linear/product_config.html", {
        "product": product,
        "form": form,
        "config": config,
        "has_instances": Linear_Instance.objects.exists(),
        "linear_enabled": services.is_enabled(),
        "baseline_preview": services.baseline_count_preview(product, push_tag) if not config.enabled else None,
        "baseline_count": product_baseline_count(product) if config.enabled else None,
    })


def product_baseline_count(product):
    from dojo.linear.models import Linear_Baseline  # noqa: PLC0415 -- lazy import, avoids circular dependency
    return Linear_Baseline.objects.filter(finding__test__engagement__product=product).count()


# --- finding actions (finding edit permission) ---------------------------------

def _findings_from_request(request):
    ids = request.POST.getlist("finding_to_update")
    findings = list(Finding.objects.filter(id__in=[i for i in ids if str(i).isdigit()]).select_related("test__engagement__product"))
    for finding in findings:
        user_has_permission_or_403(request.user, finding, "edit")
    return findings


def _report(request, issue, skipped, verb):
    if issue:
        messages.add_message(request, messages.SUCCESS, _("%(verb)s Linear issue %(id)s.") % {"verb": verb, "id": issue.identifier}, extra_tags="alert-success")
    if skipped:
        text = "; ".join(f"#{f.id} ({reason})" for f, reason in skipped)
        messages.add_message(request, messages.WARNING, _("Skipped: %(t)s") % {"t": text}, extra_tags="alert-warning")
    if not issue and not skipped:
        messages.add_message(request, messages.WARNING, _("Nothing to do."), extra_tags="alert-warning")


def _back(request, default):
    url = request.POST.get("return_url")
    safe = url and url_has_allowed_host_and_scheme(url, allowed_hosts={request.get_host()}, require_https=request.is_secure())
    return HttpResponseRedirect(url if safe else default)


def _run(request, action):
    if not services.is_enabled():
        messages.add_message(request, messages.ERROR, _("The Linear integration is disabled."), extra_tags="alert-danger")
        return
    try:
        action()
    except LinearError as e:
        logger.warning("Linear action failed: %s", e)
        messages.add_message(request, messages.ERROR, _("Linear: %(e)s") % {"e": e}, extra_tags="alert-danger")


@require_POST
def finding_linear_action(request, fid, action):
    finding = get_object_or_404(Finding, pk=fid)
    user_has_permission_or_403(request.user, finding, "edit")

    def do():
        if action == "push":
            issue, skipped = services.create_issue([finding], user=request.user)
            _report(request, issue, skipped, _("Created"))
        elif action == "link":
            issue, skipped = services.link_to_existing([finding], request.POST.get("identifier"), user=request.user)
            _report(request, issue, skipped, _("Linked to"))
        elif action == "repush":
            link = services.get_link(finding)
            if link:
                services.repush_content(link.issue)
                messages.add_message(request, messages.SUCCESS, _("Content re-pushed to %(id)s.") % {"id": link.issue.identifier}, extra_tags="alert-success")
        elif action == "unlink":
            issue = services.unlink(finding, user=request.user)
            if issue:
                messages.add_message(request, messages.SUCCESS, _("Unlinked from %(id)s.") % {"id": issue.identifier}, extra_tags="alert-success")

    _run(request, do)
    return _back(request, reverse("view_finding", args=(finding.id,)))


@require_POST
def bulk_linear_action(request):
    findings = _findings_from_request(request)
    action = request.POST.get("linear_action")

    def do():
        if not findings:
            messages.add_message(request, messages.WARNING, _("Select at least one finding."), extra_tags="alert-warning")
            return
        if action == "create":
            issue, skipped = services.create_issue(findings, user=request.user, title=request.POST.get("title") or None)
            _report(request, issue, skipped, _("Created"))
        elif action == "link":
            issue, skipped = services.link_to_existing(findings, request.POST.get("identifier"), user=request.user)
            _report(request, issue, skipped, _("Linked to"))

    _run(request, do)
    return _back(request, reverse("open_findings"))
