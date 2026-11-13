import json

from django import forms
from django.utils.translation import gettext_lazy as _

from dojo.linear.models import Linear_Instance, Linear_Product_Config


class LinearInstanceForm(forms.ModelForm):
    api_key = forms.CharField(
        required=False,
        widget=forms.PasswordInput(render_value=False),
        help_text=_("Linear personal API key (or OAuth2 access token). Stored encrypted. Leave empty to keep the current key."),
    )
    severity_priority_map = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text=_('JSON, DefectDojo severity to Linear priority (0 none, 1 urgent, 2 high, 3 medium, 4 low), e.g. {"Critical": 1, "High": 2}'),
    )

    class Meta:
        model = Linear_Instance
        fields = ["name", "auth_type", "api_key", "severity_priority_map"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["severity_priority_map"].initial = json.dumps(self.instance.severity_priority_map or {})
        if not self.instance.pk:
            self.fields["api_key"].required = True
            self.fields["api_key"].help_text = _("Linear personal API key (or OAuth2 access token). Stored encrypted.")

    def clean_severity_priority_map(self):
        raw = self.cleaned_data["severity_priority_map"]
        try:
            value = json.loads(raw)
        except ValueError:
            raise forms.ValidationError(_("Not valid JSON."))
        if not isinstance(value, dict) or not all(isinstance(v, int) and 0 <= v <= 4 for v in value.values()):
            raise forms.ValidationError(_("Must be an object of severity -> number 0..4."))
        return value

    def save(self, commit=True):  # noqa: FBT002 -- matches ModelForm.save
        # The view encrypts and sets api_key; never let the form overwrite it with "".
        api_key = self.instance.api_key
        instance = super().save(commit=False)
        instance.api_key = api_key
        if commit:
            instance.save()
        return instance


class DeleteLinearInstanceForm(forms.ModelForm):
    id = forms.IntegerField(required=True, widget=forms.widgets.HiddenInput())

    class Meta:
        model = Linear_Instance
        fields = ["id"]


class LinearProductConfigForm(forms.ModelForm):

    """Team / state / label fields become dropdowns when Linear options could be loaded."""

    label_ids = forms.MultipleChoiceField(required=False, label=_("Labels"))

    class Meta:
        model = Linear_Product_Config
        fields = ["linear_instance", "team_id", "project_id", "label_ids", "done_state_id",
                  "canceled_state_id", "reopen_state_id", "minimum_severity", "push_tag", "enabled"]
        labels = {
            "linear_instance": _("Linear connection"),
            "team_id": _("Team"),
            "project_id": _("Project id (optional)"),
            "done_state_id": _("State when mitigated (done)"),
            "canceled_state_id": _("State when false positive / out of scope / risk accepted (canceled)"),
            "reopen_state_id": _("State when reopened"),
        }

    def __init__(self, *args, teams=None, states=None, labels=None, **kwargs):
        super().__init__(*args, **kwargs)
        if teams:
            self.fields["team_id"] = forms.ChoiceField(label=_("Team"), choices=[("", "---------")] + [(t["id"], f"{t['name']} ({t['key']})") for t in teams])
        if states:
            choices = [("", "---------")] + [(s["id"], f"{s['name']} ({s['type']})") for s in states]
            for name in ("done_state_id", "canceled_state_id", "reopen_state_id"):
                self.fields[name] = forms.ChoiceField(label=self.fields[name].label, choices=choices, required=False)
        if labels:
            self.fields["label_ids"].choices = [(lb["id"], lb["name"]) for lb in labels]
        else:
            self.fields["label_ids"].choices = [(v, v) for v in (self.instance.label_ids or [])]
        for name in ("done_state_id", "canceled_state_id", "reopen_state_id"):
            self.fields[name].required = False

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("enabled"):
            missing = [self.fields[n].label for n in ("team_id", "done_state_id", "canceled_state_id", "reopen_state_id") if not cleaned.get(n)]
            if missing:
                raise forms.ValidationError(_("To enable, choose: %(fields)s.") % {"fields": ", ".join(str(m) for m in missing)})
        return cleaned
