# LINEAR-FORK: Linear issue tracker integration (parkee fork, not upstream).
# Design: Plan 3 step D (plan-3-linear-design.md). One Linear issue can hold many
# findings; a finding links to at most one issue.
from django.db import models, transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

# Linear priority: 0 none, 1 urgent, 2 high, 3 medium, 4 low.
LINEAR_PRIORITY_CHOICES = (
    (0, _("No priority")),
    (1, _("Urgent")),
    (2, _("High")),
    (3, _("Medium")),
    (4, _("Low")),
)

DEFAULT_SEVERITY_PRIORITY_MAP = {
    "Critical": 1,
    "High": 2,
    "Medium": 3,
    "Low": 4,
    "Info": 0,
}

AUTH_TYPE_API_KEY = "api_key"
AUTH_TYPE_OAUTH = "oauth"
AUTH_TYPE_CHOICES = (
    (AUTH_TYPE_API_KEY, _("Personal API key")),
    (AUTH_TYPE_OAUTH, _("OAuth2 access token")),
)

DEFAULT_PUSH_TAG = "linear"


def default_severity_priority_map():
    return dict(DEFAULT_SEVERITY_PRIORITY_MAP)


class Linear_Instance(models.Model):
    name = models.CharField(max_length=200, help_text=_("A name for this Linear workspace connection"))
    # Stored encrypted (dojo_crypto_encrypt); read with prepare_for_view. Never shown.
    api_key = models.TextField(help_text=_("Linear personal API key or OAuth2 access token. Stored encrypted."))
    auth_type = models.CharField(max_length=20, choices=AUTH_TYPE_CHOICES, default=AUTH_TYPE_API_KEY)
    workspace_name = models.CharField(max_length=200, blank=True, default="", editable=False,
                                      help_text=_("Filled in by the connection test"))
    severity_priority_map = models.JSONField(default=default_severity_priority_map,
                                             help_text=_("DefectDojo severity to Linear priority (0 none, 1 urgent, 2 high, 3 medium, 4 low)"))

    def __str__(self):
        return self.name

    def priority_for(self, severity):
        mapping = self.severity_priority_map or DEFAULT_SEVERITY_PRIORITY_MAP
        return int(mapping.get(severity, DEFAULT_SEVERITY_PRIORITY_MAP.get(severity, 0)))


class Linear_Product_Config(models.Model):
    product = models.OneToOneField("dojo.Product", on_delete=models.CASCADE, related_name="linear_config")
    linear_instance = models.ForeignKey(Linear_Instance, on_delete=models.PROTECT)
    team_id = models.CharField(max_length=64, help_text=_("Linear team new issues are created in"))
    team_name = models.CharField(max_length=200, blank=True, default="")
    project_id = models.CharField(max_length=64, blank=True, default="", help_text=_("Optional Linear project for new issues"))
    label_ids = models.JSONField(default=list, blank=True, help_text=_("Linear label ids added to new issues"))
    done_state_id = models.CharField(max_length=64, help_text=_("Linear state for issues whose findings are mitigated"))
    canceled_state_id = models.CharField(max_length=64, help_text=_("Linear state for issues whose findings are false positive, out of scope or risk accepted"))
    reopen_state_id = models.CharField(max_length=64, help_text=_("Linear state for issues whose findings become active again"))
    minimum_severity = models.CharField(max_length=20, default="Info",
                                        choices=(("Info", "Info"), ("Low", "Low"), ("Medium", "Medium"), ("High", "High"), ("Critical", "Critical")))
    push_tag = models.CharField(max_length=100, default=DEFAULT_PUSH_TAG,
                                help_text=_("Adding this tag to a verified finding creates one Linear issue for it. Findings that already carry the tag when this config is enabled are ignored."))
    enabled = models.BooleanField(default=False)
    enabled_at = models.DateTimeField(null=True, blank=True, editable=False)

    def __str__(self):
        return f"{self.product} -> {self.team_name or self.team_id}"

    def save(self, *args, **kwargs):
        # The baseline (findings already tagged before this config could act) must be
        # captured whatever saves the config: UI, admin or API. It is re-captured when
        # the config is (re-)enabled or the push tag changes.
        previous = None
        if self.pk:
            previous = Linear_Product_Config.objects.filter(pk=self.pk).values("enabled", "push_tag").first()
        turning_on = self.enabled and (previous is None or not previous["enabled"])
        tag_changed = self.enabled and previous is not None and previous["push_tag"] != self.push_tag
        self.push_tag = (self.push_tag or "").strip().lower()
        if turning_on:
            self.enabled_at = timezone.now()
        if not self.enabled:
            self.enabled_at = None
        with transaction.atomic():
            super().save(*args, **kwargs)
            if turning_on or tag_changed:
                from dojo.linear.services import capture_baseline  # noqa: PLC0415 -- lazy import, avoids circular dependency
                capture_baseline(self)


class Linear_Issue(models.Model):
    linear_instance = models.ForeignKey(Linear_Instance, on_delete=models.PROTECT)
    linear_id = models.CharField(max_length=64)
    identifier = models.CharField(max_length=64, help_text=_("e.g. SEC-42"))
    url = models.URLField(max_length=2000)
    team_id = models.CharField(max_length=64, blank=True, default="")
    created_by_dd = models.BooleanField(default=True, help_text=_("False when an existing (hand-made) Linear issue was linked"))
    created = models.DateTimeField(auto_now_add=True)
    last_status_pushed = models.CharField(max_length=64, blank=True, default="",
                                          help_text=_("Last Linear state id DefectDojo set"))

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["linear_instance", "linear_id"], name="linear_issue_unique_per_instance"),
        ]

    def __str__(self):
        return self.identifier


class Linear_Finding_Link(models.Model):
    finding = models.OneToOneField("dojo.Finding", on_delete=models.CASCADE, related_name="linear_link")
    issue = models.ForeignKey(Linear_Issue, on_delete=models.CASCADE, related_name="finding_links")
    linked_by = models.ForeignKey("dojo.Dojo_User", null=True, blank=True, on_delete=models.SET_NULL)
    linked_at = models.DateTimeField(auto_now_add=True)
    dd_note = models.ForeignKey("dojo.Notes", null=True, blank=True, on_delete=models.SET_NULL)
    linear_attachment_id = models.CharField(max_length=64, blank=True, default="")

    def __str__(self):
        return f"Finding {self.finding_id} -> {self.issue}"


class Linear_Baseline(models.Model):
    # Findings that already carried the push tag when Linear was enabled for their
    # product. Their issues were made by hand; DD never creates an issue for them.
    finding = models.OneToOneField("dojo.Finding", on_delete=models.CASCADE, related_name="linear_baseline")
    recorded_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Baseline finding {self.finding_id}"
