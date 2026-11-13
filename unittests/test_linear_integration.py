# LINEAR-FORK: tests for dojo/linear (the Linear API is always mocked).
from unittest.mock import MagicMock, patch

import crum
from django.utils import timezone

from dojo.linear import services
from dojo.linear.client import LinearError
from dojo.linear.models import (
    Linear_Baseline,
    Linear_Finding_Link,
    Linear_Instance,
    Linear_Issue,
    Linear_Product_Config,
)
from dojo.models import Finding, Product, User, UserContactInfo
from dojo.utils import dojo_crypto_encrypt
from unittests.dojo_test_case import DojoTestCase, versioned_fixtures


def fake_client():
    client = MagicMock()
    counter = {"n": 0}

    def create_issue(issue_input):
        counter["n"] += 1
        n = counter["n"]
        return {"id": f"uuid-{n}", "identifier": f"SEC-{n}", "url": f"https://linear.app/acme/issue/SEC-{n}"}

    client.create_issue.side_effect = create_issue
    client.create_attachment.return_value = "att-1"
    client.get_issue.return_value = {
        "id": "uuid-hand", "identifier": "SEC-99", "url": "https://linear.app/acme/issue/SEC-99",
        "description": "Written by a person.\n\nMore human text.", "team": {"id": "team-1"},
    }
    return client


@versioned_fixtures
class LinearTestBase(DojoTestCase):
    fixtures = ["dojo_testdata.json"]

    def setUp(self):
        super().setUp()
        self.system_settings(enable_linear=True)
        self.user = User.objects.get(username="admin")
        self.instance = Linear_Instance.objects.create(name="acme", api_key=dojo_crypto_encrypt("lin_api_x"))
        product_ids = Finding.objects.values_list("test__engagement__product", flat=True).distinct()
        self.product = Product.objects.get(pk=product_ids[0])
        self.findings = list(Finding.objects.filter(test__engagement__product=self.product).order_by("id"))
        for f in self.findings:
            Finding.objects.filter(pk=f.pk).update(active=True, verified=True, duplicate=False, false_p=False,
                                                   out_of_scope=False, risk_accepted=False, is_mitigated=False, severity="High")
            f.refresh_from_db()
        self.config = self.make_config(self.product)
        self.client_mock = fake_client()
        patcher = patch("dojo.linear.services.LinearClient")
        self.client_cls = patcher.start()
        self.client_cls.for_instance.return_value = self.client_mock
        self.addCleanup(patcher.stop)

    def make_config(self, product):
        return Linear_Product_Config.objects.create(
            product=product, linear_instance=self.instance, team_id="team-1",
            done_state_id="state-done", canceled_state_id="state-canceled", reopen_state_id="state-todo",
            enabled=True,
        )

    def tag(self, finding, tag="linear"):
        finding.tags.add(tag)
        finding.refresh_from_db()


class LinearTagTriggerTest(LinearTestBase):

    def hook(self, finding):
        with patch("dojo.celery_dispatch.dojo_dispatch_task") as dispatch:
            services.sync_if_needed(finding)
        return dispatch

    def test_verified_but_untagged_does_nothing(self):
        self.assertFalse(self.hook(self.findings[0]).called)

    def test_tag_on_unverified_does_nothing(self):
        f = self.findings[0]
        Finding.objects.filter(pk=f.pk).update(verified=False)
        f.refresh_from_db()
        self.tag(f)
        self.assertFalse(self.hook(f).called)

    def test_tag_and_verified_dispatches_create(self):
        f = self.findings[0]
        self.tag(f)
        dispatch = self.hook(f)
        self.assertTrue(dispatch.called)
        self.assertEqual(dispatch.call_args.args[1], [f.id])

    def test_flag_off_does_nothing(self):
        self.system_settings(enable_linear=False)
        f = self.findings[0]
        self.tag(f)
        self.assertFalse(self.hook(f).called)

    def test_baselined_finding_never_dispatches(self):
        f = self.findings[0]
        Linear_Baseline.objects.create(finding=f)
        self.tag(f)
        self.assertFalse(self.hook(f).called)
        # removing and re-adding the tag keeps it baselined
        f.tags.remove("linear")
        self.tag(f)
        self.assertFalse(self.hook(f).called)

    def test_linked_finding_only_syncs_status(self):
        issue, _ = services.create_issue([self.findings[0]], user=self.user)
        f = Finding.objects.get(pk=self.findings[0].pk)
        f.tags.remove("linear")  # removing the tag after the push is a no-op for the link
        dispatch = self.hook(f)
        self.assertEqual(dispatch.call_args.args[0].__name__, "sync_issue_status_task")
        self.assertTrue(Linear_Finding_Link.objects.filter(finding=f, issue=issue).exists())

    def test_hook_never_raises(self):
        with patch("dojo.linear.services.get_link", side_effect=RuntimeError("boom")):
            services.sync_if_needed(self.findings[0])  # must not raise

    def test_bulk_tag_path_runs_hook_per_finding(self):
        for f in self.findings[:2]:
            self.tag(f)
        with patch("dojo.celery_dispatch.dojo_dispatch_task") as dispatch:
            services.sync_findings_if_needed(self.findings[:2])
        self.assertEqual(dispatch.call_count, 2)


class LinearBaselineTest(LinearTestBase):

    def test_baseline_captured_when_enabled(self):
        self.config.enabled = False
        self.config.save()
        Linear_Baseline.objects.all().delete()
        self.tag(self.findings[0])
        self.config.enabled = True
        self.config.save()
        self.assertTrue(Linear_Baseline.objects.filter(finding=self.findings[0]).exists())
        self.assertFalse(Linear_Baseline.objects.filter(finding=self.findings[1]).exists())

    def test_baseline_captured_when_push_tag_changes(self):
        self.tag(self.findings[1], "to-linear")
        self.config.push_tag = "to-linear"
        self.config.save()
        self.assertTrue(Linear_Baseline.objects.filter(finding=self.findings[1]).exists())

    def test_tag_added_after_enable_is_not_baselined(self):
        self.tag(self.findings[0])
        self.assertFalse(Linear_Baseline.objects.filter(finding=self.findings[0]).exists())


class LinearCreateTest(LinearTestBase):

    def test_create_single_writes_issue_link_note_attachment(self):
        f = self.findings[0]
        issue, skipped = services.create_issue([f], user=self.user)
        self.assertEqual(skipped, [])
        self.assertEqual(issue.identifier, "SEC-1")
        link = Linear_Finding_Link.objects.get(finding=f)
        self.assertEqual(link.issue, issue)
        self.assertIn("SEC-1", link.dd_note.entry)
        self.assertEqual(link.linear_attachment_id, "att-1")
        self.assertIn("linear", services.finding_tag_names(f))
        sent = self.client_mock.create_issue.call_args.args[0]
        self.assertEqual(sent["teamId"], "team-1")
        self.assertEqual(sent["priority"], 2)  # High
        # DefectDojo reference is at the bottom
        self.assertTrue(sent["description"].rstrip().splitlines()[-1].startswith(f"- [#{f.id} "))
        self.assertIn("\n---\n**DefectDojo**\n", sent["description"])

    def test_create_several_makes_one_issue(self):
        issue, _ = services.create_issue(self.findings[:2], user=self.user, title="Two in one")
        self.assertEqual(self.client_mock.create_issue.call_count, 1)
        self.assertEqual(issue.finding_links.count(), 2)
        self.assertEqual(self.client_mock.create_issue.call_args.args[0]["title"], "Two in one")
        for f in self.findings[:2]:
            self.assertIn("linear", services.finding_tag_names(f))

    def test_create_across_products_same_workspace(self):
        other = Product.objects.exclude(pk=self.product.pk).filter(engagement__test__finding__isnull=False).distinct().first()
        if other is None:
            self.skipTest("fixture has a single product with findings")
        self.make_config(other)
        f2 = Finding.objects.filter(test__engagement__product=other).first()
        Finding.objects.filter(pk=f2.pk).update(active=True, verified=True, duplicate=False)
        issue, skipped = services.create_issue([self.findings[0], Finding.objects.get(pk=f2.pk)], user=self.user)
        self.assertEqual(skipped, [])
        self.assertEqual(issue.finding_links.count(), 2)

    def test_already_linked_findings_are_skipped(self):
        services.create_issue([self.findings[0]], user=self.user)
        issue, skipped = services.create_issue([self.findings[0]], user=self.user)
        self.assertIsNone(issue)
        self.assertEqual(skipped[0][1], "already linked to a Linear issue")
        self.assertEqual(self.client_mock.create_issue.call_count, 1)

    def test_tag_path_skips_baselined(self):
        Linear_Baseline.objects.create(finding=self.findings[0])
        issue, _ = services.create_issue([self.findings[0]], from_tag=True)
        self.assertIsNone(issue)
        self.assertFalse(self.client_mock.create_issue.called)

    def test_retry_of_links_does_not_duplicate(self):
        issue, _ = services.create_issue([self.findings[0]], user=self.user)
        notes_before = self.findings[0].notes.count()
        services.repair_links(issue)
        self.assertEqual(self.findings[0].notes.count(), notes_before)
        self.assertEqual(self.client_mock.create_attachment.call_count, 1)

    def test_link_failure_keeps_the_issue(self):
        self.client_mock.create_attachment.side_effect = LinearError("attachment down")
        issue, _ = services.create_issue([self.findings[0]], user=self.user)
        self.assertIsNotNone(issue)
        self.assertEqual(Linear_Finding_Link.objects.get(finding=self.findings[0]).linear_attachment_id, "")

    def test_api_failure_creates_nothing(self):
        self.client_mock.create_issue.side_effect = LinearError("bad key")
        with self.assertRaises(LinearError):
            services.create_issue([self.findings[0]], user=self.user)
        self.assertFalse(Linear_Issue.objects.exists())
        self.assertFalse(Linear_Finding_Link.objects.exists())

    def test_unverified_finding_is_skipped(self):
        Finding.objects.filter(pk=self.findings[0].pk).update(verified=False)
        issue, skipped = services.create_issue([Finding.objects.get(pk=self.findings[0].pk)])
        self.assertIsNone(issue)
        self.assertEqual(skipped[0][1], "not verified")


class LinearLinkExistingTest(LinearTestBase):

    def test_link_to_existing_creates_nothing_and_keeps_human_text(self):
        issue, skipped = services.link_to_existing(self.findings[:2], "SEC-99", user=self.user)
        self.assertEqual(skipped, [])
        self.assertFalse(self.client_mock.create_issue.called)
        self.assertFalse(issue.created_by_dd)
        self.assertEqual(issue.finding_links.count(), 2)
        description = self.client_mock.update_issue.call_args_list[0].args[1]["description"]
        self.assertTrue(description.startswith("Written by a person.\n\nMore human text."))
        self.assertIn("**DefectDojo**", description)
        for f in self.findings[:2]:
            self.assertIn("linear", services.finding_tag_names(f))
        self.assertTrue(self.client_mock.create_comment.called)

    def test_unknown_identifier_raises(self):
        self.client_mock.get_issue.return_value = None
        with self.assertRaises(LinearError):
            services.link_to_existing([self.findings[0]], "SEC-404")
        self.assertFalse(Linear_Finding_Link.objects.exists())

    def test_replace_dd_section_only_touches_bottom(self):
        f = self.findings[0]
        original = "Human text.\n\n---\n**DefectDojo**\n- [#1 old](x): p / Low / Verified"
        result = services.replace_dd_section(original, [f])
        self.assertTrue(result.startswith("Human text.\n\n---\n**DefectDojo**\n"))
        self.assertNotIn("#1 old", result)
        self.assertIn(f"#{f.id}", result)
        self.assertEqual(result.count("**DefectDojo**"), 1)

    def test_secret_values_never_reach_linear(self):
        f = self.findings[0]
        Finding.objects.filter(pk=f.pk).update(description="**Secret:** AKIAABCDEFGH1234\n**Match:** key = AKIAABCDEFGH1234\n**Rule Id:** aws")
        f.refresh_from_db()
        body = services.render_content([f])
        self.assertNotIn("AKIAABCDEFGH1234", body)
        self.assertIn("**Secret:** REDACTED", body)
        self.assertIn("**Match:** REDACTED", body)
        self.assertIn("**Rule Id:** aws", body)
        already = "**Secret:** REDACTED\n**Match:** key = REDACTED"
        self.assertEqual(services.redact_secrets(already), already)

    def test_replace_dd_section_appends_when_absent(self):
        result = services.replace_dd_section("Only human text.", [self.findings[0]])
        self.assertTrue(result.startswith("Only human text.\n\n---\n**DefectDojo**"))


class LinearStatusTest(LinearTestBase):

    def set(self, finding, **fields):
        Finding.objects.filter(pk=finding.pk).update(**fields)
        finding.refresh_from_db()

    def test_aggregate_state(self):
        a, b = self.findings[:2]
        self.assertEqual(services.aggregate_state([a, b]), "open")
        self.set(a, active=False, is_mitigated=True, mitigated=timezone.now())
        self.assertEqual(services.aggregate_state([a, b]), "open")
        self.set(b, active=False, false_p=True)
        self.assertEqual(services.aggregate_state([a, b]), "done")
        self.set(a, is_mitigated=False, mitigated=None, out_of_scope=True)
        self.assertEqual(services.aggregate_state([a, b]), "canceled")

    def test_issue_closes_only_when_all_closed_and_reopens(self):
        a, b = self.findings[:2]
        issue, _ = services.create_issue([a, b], user=self.user)
        self.client_mock.update_issue.reset_mock()
        self.set(a, active=False, is_mitigated=True)
        services.sync_issue_status(issue, changed_finding=a)
        self.assertFalse(self.client_mock.update_issue.called)  # b still open
        self.set(b, active=False, is_mitigated=True)
        services.sync_issue_status(issue, changed_finding=b)
        self.client_mock.update_issue.assert_called_once_with("uuid-1", {"stateId": "state-done"})
        self.client_mock.update_issue.reset_mock()
        services.sync_issue_status(issue)  # unchanged -> no call
        self.assertFalse(self.client_mock.update_issue.called)
        self.set(a, active=True, is_mitigated=False)
        services.sync_issue_status(issue, changed_finding=a)
        self.client_mock.update_issue.assert_called_once_with("uuid-1", {"stateId": "state-todo"})

    def test_all_false_positive_cancels(self):
        a = self.findings[0]
        issue, _ = services.create_issue([a], user=self.user)
        self.set(a, active=False, false_p=True)
        services.sync_issue_status(issue, changed_finding=a)
        self.client_mock.update_issue.assert_called_with("uuid-1", {"stateId": "state-canceled"})

    def test_open_issue_not_touched_when_dd_never_closed_it(self):
        issue, _ = services.link_to_existing([self.findings[0]], "SEC-99", user=self.user)
        self.client_mock.update_issue.reset_mock()
        services.sync_issue_status(issue)
        self.assertFalse(self.client_mock.update_issue.called)

    def test_deleting_last_finding_cancels_issue(self):
        a = self.findings[0]
        issue, _ = services.create_issue([a], user=self.user)
        Linear_Finding_Link.objects.filter(issue=issue).delete()
        services.finding_deleted(issue, f"#{a.id} {a.title}", self.product.id)
        self.client_mock.update_issue.assert_called_with("uuid-1", {"stateId": "state-canceled"})

    def test_status_only_after_creation(self):
        a = self.findings[0]
        issue, _ = services.create_issue([a], user=self.user)
        self.client_mock.update_issue.reset_mock()
        self.set(a, title="Edited title")
        services.sync_issue_status(issue, changed_finding=a)
        self.assertFalse(self.client_mock.update_issue.called)
        services.repush_content(issue)
        self.assertEqual(self.client_mock.update_issue.call_args.args[1]["title"], "Edited title")


class LinearUnlinkTest(LinearTestBase):

    def test_unlink_removes_link_and_tag(self):
        a = self.findings[0]
        issue, _ = services.create_issue([a], user=self.user)
        services.unlink(a, user=self.user)
        self.assertFalse(Linear_Finding_Link.objects.filter(finding=a).exists())
        self.assertNotIn("linear", services.finding_tag_names(Finding.objects.get(pk=a.pk)))
        self.assertTrue(Linear_Issue.objects.filter(pk=issue.pk).exists())


class LinearSavePathTest(LinearTestBase):

    """End to end through Finding.save() -> post_process_finding_save -> the hook -> the task."""

    def setUp(self):
        super().setUp()
        # Run dispatched tasks in the foreground, as test_async_delete.py does.
        UserContactInfo.objects.update_or_create(user=self.user, defaults={"block_execution": True})
        self.enterContext(crum.impersonate(self.user))

    def test_tag_then_save_creates_issue_and_verify_alone_does_not(self):
        f = Finding.objects.get(pk=self.findings[0].pk)
        Finding.objects.filter(pk=f.pk).update(verified=False)
        f.refresh_from_db()
        f.verified = True
        f.save()  # verifying alone: nothing
        self.assertFalse(self.client_mock.create_issue.called)
        f.tags.add("linear")
        f.save()  # tag + verified: one issue
        self.assertEqual(self.client_mock.create_issue.call_count, 1)
        self.assertTrue(Linear_Finding_Link.objects.filter(finding=f).exists())
        f.title = "edited after push"
        f.save()  # later edits: status only, no new issue
        self.assertEqual(self.client_mock.create_issue.call_count, 1)

    def test_mitigating_a_linked_finding_closes_the_issue(self):
        f = Finding.objects.get(pk=self.findings[0].pk)
        services.create_issue([f], user=self.user)
        self.client_mock.update_issue.reset_mock()
        f = Finding.objects.get(pk=f.pk)
        f.active = False
        f.is_mitigated = True
        f.mitigated = timezone.now()
        f.save()
        self.client_mock.update_issue.assert_called_with("uuid-1", {"stateId": "state-done"})
