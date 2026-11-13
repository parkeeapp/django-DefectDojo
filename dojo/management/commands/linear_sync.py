# LINEAR-FORK: maintenance for the Linear integration.
from django.core.management.base import BaseCommand, CommandError

from dojo.linear import services
from dojo.linear.models import Linear_Baseline, Linear_Issue, Linear_Product_Config
from dojo.models import Finding, Product


class Command(BaseCommand):
    help = (
        "Linear integration maintenance. Without options: create issues for findings of a product that are "
        "tagged, verified, not linked and not baselined (e.g. after an outage). Use --dry-run first."
    )

    def add_arguments(self, parser):
        parser.add_argument("--product", type=int, help="Product id")
        parser.add_argument("--dry-run", action="store_true", help="Only list what would be pushed")
        parser.add_argument("--show-baseline", action="store_true", help="List findings ignored because they were tagged before Linear was enabled")
        parser.add_argument("--repair-links", action="store_true", help="Write missing DD notes / Linear link cards for linked findings")

    def handle(self, *args, **options):
        if not services.is_enabled():
            msg = "The Linear integration is disabled in System Settings."
            raise CommandError(msg)
        product = None
        if options["product"]:
            product = Product.objects.filter(pk=options["product"]).first()
            if not product:
                msg = f"Product {options['product']} not found"
                raise CommandError(msg)

        if options["show_baseline"]:
            qs = Linear_Baseline.objects.select_related("finding")
            if product:
                qs = qs.filter(finding__test__engagement__product=product)
            for row in qs:
                self.stdout.write(f"#{row.finding_id}\t{row.finding.title}\t(recorded {row.recorded_at:%Y-%m-%d})")
            return

        if options["repair_links"]:
            qs = Linear_Issue.objects.all()
            if product:
                qs = qs.filter(finding_links__finding__test__engagement__product=product).distinct()
            for issue in qs:
                services.repair_links(issue)
                self.stdout.write(f"checked {issue.identifier}")
            return

        if not product:
            msg = "--product is required"
            raise CommandError(msg)
        config = Linear_Product_Config.objects.filter(product=product, enabled=True).first()
        if not config:
            msg = "Linear is not enabled for this product"
            raise CommandError(msg)
        candidates = (
            Finding.objects.filter(test__engagement__product=product, tags__name=config.push_tag)
            .filter(linear_link__isnull=True, linear_baseline__isnull=True)
            .distinct()
        )
        for finding in candidates:
            problem = services.eligibility_problem(finding, config)
            if problem:
                self.stdout.write(f"skip #{finding.id}: {problem}")
                continue
            if options["dry_run"]:
                self.stdout.write(f"would push #{finding.id} {finding.title}")
                continue
            issue, _ = services.create_issue([finding], from_tag=True)
            self.stdout.write(f"#{finding.id} -> {issue.identifier if issue else 'not created'}")
