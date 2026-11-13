# Linear integration (parkee fork): upgrade notes

The Linear integration is a fork-only feature. It lives in `dojo/linear/` and touches
upstream files in the places listed below, each marked `LINEAR-FORK` in the code
(`grep -rn LINEAR-FORK dojo/`). When rebasing onto a new upstream release, re-check
every line in this table.

Design: Plan 3 step D (`plan-3-linear-design.md` in the defectDojo-setup workspace).

## What it does

- Tag `linear` (per-product push tag) on an **active + verified** finding -> one Linear
  issue for that finding. Verifying alone never pushes.
- Bulk Edit / finding page -> "Create one Linear issue from selected" or "Link to existing
  Linear issue": several findings on one issue; the tag is added after linking.
- Findings that already had the tag when a product was enabled go into `Linear_Baseline`
  and are never pushed (their issues were made by hand).
- Two-way links: a DD note per finding; a DefectDojo section at the **bottom** of the
  issue description plus one sidebar link card per finding.
- After creation, **status only** is synced (any open -> open; all closed with one fixed
  -> done; all FP / out of scope / risk accepted -> canceled). DD only reopens an issue
  DD itself closed. "Re-push content" overwrites title/description/priority on purpose.
- Off by default: `System_Settings.enable_linear`. With it off, the hook returns before
  any query and every Linear menu, panel and bulk form is hidden.

## Touchpoints in upstream files

| # | File | Change |
|---|---|---|
| 1 | `dojo/system_settings/models.py` (after `enable_github`) | `enable_linear` field (rendered by the generic System Settings form, no template change) |
| 2 | `dojo/db_migrations/0299_linear_integration.py` | new, additive: 5 tables + 1 column |
| 3 | `dojo/models.py` (re-export block after `dojo.jira.models`) | re-export the Linear models |
| 4 | `dojo/finding/helper.py` `post_process_finding_save_internal` (after the Jira push) | `linear_services.sync_if_needed(finding)` |
| 5 | `dojo/finding/helper.py` `finding_delete` (after the Jira close) | `linear_services.on_finding_deleted(instance)` |
| 6 | `dojo/finding/ui/views.py` bulk edit, after `bulk_add_tags_to_instances` | `linear_services.sync_findings_if_needed(finds)` (bulk tagging skips `save()`) |
| 7 | `dojo/urls.py` | imports; `ur += linear_urls`; `api/v2/linear/push/` and `api/v2/linear/link/` in `api_v2_urls` |
| 8 | `dojo/settings/settings.dist.py` `CELERY_IMPORTS` | add `dojo.linear.tasks` (otherwise the worker does not register the tasks) |
| 9 | `dojo/templates/base.html` | Configuration menu entry "Linear"; product Settings menu entry "Linear Settings" |
| 10 | `dojo/templates/dojo/view_finding.html` | `{% load linear_tags %}` and `{% linear_finding_panel finding %}` |
| 11 | `dojo/templates/dojo/findings_list_snippet.html` | `{% include "dojo/linear/bulk_actions.html" %}` after the Bulk Edit form |
| 12 | `dojo/templates/dojo/view_test.html` | same include after its Bulk Edit form |

New files only (no upstream equivalent): `dojo/linear/**`, `dojo/templatetags/linear_tags.py`,
`dojo/management/commands/linear_sync.py`, `dojo/templates/dojo/linear/*`,
`dojo/templates/issue-trackers/linear/*`, `unittests/test_linear_integration.py`.

Permissions: the Linear URLs are not in `dojo/authorization/url_permissions.py`; the views
check permissions themselves (configuration permission `dojo.*_linear_instance` for
connections, product **edit** for product settings, finding **edit** for finding actions).

## Rebase checklist

1. `git rebase` / merge the new upstream release.
2. For each touchpoint above, confirm the surrounding code still exists and the call is
   still reached (upstream moves code during its module reorganisation).
3. Migration collision (below).
4. `ruff check --config ruff.toml dojo/linear dojo/templatetags/linear_tags.py dojo/management/commands/linear_sync.py`
5. `docker compose exec -T uwsgi python manage.py makemigrations --check --dry-run` -> "No changes detected".
6. `./run-unittest.sh --test-case unittests.test_linear_integration`
7. Also run the upstream suites that cover the touched code: `test_finding_helper`,
   `test_finding_model`, `test_bulk_edit_validation`, `test_jira_*`, `test_tags`,
   `test_tag_utils_bulk`, `test_importers_performance` (query counts).

## Migration collision playbook

Our migration is `0299_linear_integration`, depending on upstream's `0298`. If upstream
ships its own `0299_*`:

1. Rename ours to the next free number, e.g. `0300_linear_integration.py`, and set its
   `dependencies` to upstream's latest migration.
2. `makemigrations --check --dry-run` must print "No changes detected".
3. **Fresh database:** nothing else to do.
4. **Database that already applied ours under the old name** (e.g. the VM): back up the
   database first, then tell Django the renamed migration is already applied, without
   running it again:
   ```bash
   docker compose exec -T uwsgi python manage.py shell -c "from django.db.migrations.recorder import MigrationRecorder as R; R.Migration.objects.filter(app='dojo', name='0299_linear_integration').update(name='0300_linear_integration')"
   docker compose exec -T uwsgi python manage.py migrate --plan   # upstream's 0299 must show as pending, ours as applied
   docker compose exec -T uwsgi python manage.py migrate
   ```
   Our migration only creates new tables and one column, so upstream's migration does not
   depend on it and the order between them does not matter.

## Things to re-check once a real Linear key exists (plan step E5)

- `issue(id:)` accepts the human identifier (`SEC-42`).
- `attachmentCreate` field names, and how link cards look.
- Linear keeps the `---` rule + `**DefectDojo**` heading, so the bottom-section rewrite
  (`DD_SECTION_RE` in `services.py`) finds it.
- The DD links use `SITE_URL`; it must be the public DefectDojo URL on the VM.
