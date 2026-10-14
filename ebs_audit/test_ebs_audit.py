import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import boto3
from botocore.stub import Stubber

import ebs_audit as audit

NOW = datetime(2026, 10, 14, tzinfo=timezone.utc)


def volume(**changes):
    result = {"VolumeId": "vol-0123456789abcdef0", "State": "available",
              "Attachments": [], "CreateTime": NOW - timedelta(days=31),
              "VolumeType": "gp3", "Size": 100, "Iops": 3000,
              "Throughput": 125, "AvailabilityZone": "us-east-1a"}
    result.update(changes)
    return result


def client(service):
    return boto3.client(service, region_name="us-east-1",
                        aws_access_key_id="testing", aws_secret_access_key="testing")


def product(rate="0.08", usage="EBS:VolumeUsage.gp3", unit="GB-Mo"):
    return json.dumps({"product": {"sku": "test-sku", "attributes": {
        "locationType": "AWS Region", "usagetype": usage}},
        "terms": {"OnDemand": {"test": {"effectiveDate": "2026-01-01T00:00:00Z",
            "priceDimensions": {"test-rate": {"unit": unit, "beginRange": "0",
                "endRange": "Inf", "pricePerUnit": {"USD": rate}}}}}}})


class AuditTests(unittest.TestCase):
    def test_age_boundary_and_attachment_state(self):
        self.assertTrue(audit.old_unattached(volume(), NOW, 30))
        for v in [volume(CreateTime=NOW - timedelta(days=30)),
                  volume(CreateTime=NOW - timedelta(days=29)),
                  volume(State="in-use"), volume(Attachments=[{"State": "attached"}])]:
            self.assertFalse(audit.old_unattached(v, NOW, 30))

    def test_protection_presence_case_and_required_tag(self):
        v = volume(Tags=[{"Key": "donotdelete", "Value": "false"}])
        self.assertIn("protection_tag_present", audit.plan_exclusions(v, ["DoNotDelete"], None))
        self.assertIn("required_plan_tag_missing", audit.plan_exclusions(v, [], "CleanupApproved=true"))
        self.assertEqual([], audit.plan_exclusions(volume(Tags=[{"Key": "CleanupApproved", "Value": "true"}]), [], "CleanupApproved=true"))

    def test_managed_and_outposts_excluded(self):
        self.assertIn("aws_managed_volume", audit.plan_exclusions(volume(Operator={"Managed": True}), [], None))
        self.assertIn("outposts_requires_separate_review", audit.plan_exclusions(volume(OutpostArn="test"), [], None))

    def test_organizations_pagination_state_and_inactive(self):
        org = client("organizations")
        with Stubber(org) as stub:
            # Empty pages with a token must not terminate discovery.
            stub.add_response("list_accounts", {"Accounts": [], "NextToken": "next"}, {})
            stub.add_response("list_accounts", {"Accounts": [
                {"Id": "111111111111", "Name": "active", "State": "ACTIVE"},
                {"Id": "222222222222", "Name": "closed", "State": "CLOSED"}]}, {"NextToken": "next"})
            active, skipped = audit.discover_accounts(org, set())
            self.assertEqual([a["Id"] for a in active], ["111111111111"])
            self.assertEqual(skipped[0]["state"], "CLOSED")
            stub.assert_no_pending_responses()

    def test_unknown_account_fails(self):
        org = Mock()
        org.get_paginator.return_value.paginate.return_value = [{"Accounts": []}]
        with self.assertRaises(ValueError):
            audit.discover_accounts(org, {"111111111111"})

    def test_price_lookup_pagination_and_cache(self):
        pricing = client("pricing")
        params = {"ServiceCode": "AmazonEC2", "FormatVersion": "aws_v1", "Filters": [
            {"Type": "TERM_MATCH", "Field": "regionCode", "Value": "us-east-1"},
            {"Type": "TERM_MATCH", "Field": "volumeApiName", "Value": "gp3"}]}
        with Stubber(pricing) as stub:
            stub.add_response("get_products", {"PriceList": [], "NextToken": "next"}, params)
            stub.add_response("get_products", {"PriceList": [product()]}, {**params, "NextToken": "next"})
            prices = audit.Prices(pricing)
            self.assertEqual(prices.estimate("us-east-1", volume())[2], Decimal("8.00"))
            self.assertEqual(prices.estimate("us-east-1", volume())[2], Decimal("8.00"))
            stub.assert_no_pending_responses()

    def test_ambiguous_price_is_not_zero(self):
        pricing = Mock()
        pricing.get_paginator.return_value.paginate.return_value = [{"PriceList": [product(), product("0.09")]}]
        estimate = audit.Prices(pricing).estimate("us-east-1", volume())
        self.assertIsNone(estimate[2])
        self.assertEqual(estimate[3], "unknown")

    def test_missing_performance_price_preserves_storage(self):
        prices = audit.Prices(Mock())
        prices.lookup = Mock(side_effect=[(Decimal("0.08"), "EBS:VolumeUsage.gp3"), ValueError("missing")])
        storage, performance, total, status, _ = prices.estimate("us-east-1", volume(Iops=5000))
        self.assertEqual(storage, Decimal("8"))
        self.assertIsNone(total)
        self.assertIsNone(performance)
        self.assertEqual(status, "partial")

    def test_gp3_throughput_unit_conversion_and_iops(self):
        prices = audit.Prices(Mock())
        rates = {"gp3": (Decimal("0.08"), "EBS:VolumeUsage.gp3"),
                 "EBS:VolumeP-IOPS.gp3": (Decimal("0.005"), "test"),
                 "EBS:VolumeP-Throughput.gp3": (Decimal("40.96"), "test")}
        prices.lookup = lambda region, field, value, unit: rates[value]
        self.assertEqual(prices.estimate("us-east-1", volume(Iops=5000, Throughput=250))[2], Decimal("23"))

    def test_io2_tier_boundaries(self):
        for iops, expected in [(32000, "2080"), (64000, "3552"), (65000, "3584")]:
            prices = audit.Prices(Mock())
            rates = {"io2": (Decimal("0.125"), "EBS:VolumeUsage.io2"),
                     "EBS:VolumeP-IOPS.io2": (Decimal("0.065"), "test"),
                     "EBS:VolumeP-IOPS.io2.tier2": (Decimal("0.046"), "test"),
                     "EBS:VolumeP-IOPS.io2.tier3": (Decimal("0.032"), "test")}
            prices.lookup = lambda region, field, value, unit: rates[value]
            self.assertEqual(prices.estimate("us-east-1", volume(VolumeType="io2", Iops=iops))[1], Decimal(expected))

    def test_local_zone_not_priced_as_parent_region(self):
        prices = audit.Prices(Mock())
        self.assertEqual(prices.estimate("us-west-2", volume(AvailabilityZone="us-west-2-lax-1a"))[3], "unknown")

    def test_ec2_pagination_and_plan_exclusions(self):
        ec2 = client("ec2")
        args = SimpleNamespace(days=30, protect_tag_key=["Keep"], plan_require_tag=None)
        prices = Mock()
        prices.estimate.return_value = (Decimal(8), Decimal(0), Decimal(8), "estimated", "")
        rows = []
        params = {"Filters": [{"Name": "status", "Values": ["available"]}], "IncludeManagedResources": True}
        with Stubber(ec2) as stub:
            stub.add_response("describe_volumes", {"Volumes": [volume()], "NextToken": "next"}, params)
            stub.add_response("describe_volumes", {"Volumes": [volume(VolumeId="vol-11111111111111111", Tags=[{"Key": "Keep", "Value": "yes"}])]}, {**params, "NextToken": "next"})
            audit.scan_region(ec2, {"Id": "111111111111", "Name": "dev"}, "us-east-1", NOW, args, prices, rows)
            stub.assert_no_pending_responses()
        self.assertEqual(len(rows), 2)
        plan = audit.deletion_plan(rows, NOW, args)
        self.assertEqual(len(plan["items"]), 1)
        self.assertEqual(plan["mode"], "review_only")
        self.assertEqual(audit.summarize(rows)["overall"]["known_plan_eligible_monthly_usd"], "8.000000")

    def test_partial_scan_still_writes_report_and_exit_two(self):
        base = Mock()
        sts, org, pricing = Mock(), Mock(), Mock()
        sts.get_caller_identity.return_value = {"Account": "111111111111", "Arn": "arn:aws:iam::111111111111:role/Audit"}
        sts.assume_role.side_effect = audit.ClientError({"Error": {"Code": "AccessDenied", "Message": "denied"}}, "AssumeRole")
        base.client.side_effect = lambda service, **kw: {"sts": sts, "organizations": org, "pricing": pricing}[service]
        org.get_paginator.return_value.paginate.return_value = [{"Accounts": [{"Id": "222222222222", "Name": "dev", "State": "ACTIVE"}]}]
        with tempfile.TemporaryDirectory() as tmp, patch.object(audit.boto3, "Session", return_value=base):
            self.assertEqual(audit.main(["--output-dir", tmp, "--deletion-plan"]), 2)
            report = json.loads((Path(tmp) / "report.json").read_text())
            self.assertFalse(report["inventory_complete"])
            self.assertEqual(report["errors"][0]["stage"], "account_access_or_region_discovery")
            self.assertTrue((Path(tmp) / "deletion-plan.json").exists())

    def test_assume_role_uses_partition_and_external_id(self):
        sts = Mock()
        sts.assume_role.return_value = {"Credentials": {"AccessKeyId": "test", "SecretAccessKey": "test", "SessionToken": "test"}}
        args = SimpleNamespace(role_name="audit/EBSInventoryRole", external_id="audit-id",
                               home_region="us-gov-west-1", use_current_account_credentials=False)
        with patch.object(audit.boto3, "Session"):
            audit.account_session(Mock(), sts, "222222222222", "111111111111", "aws-us-gov", args)
        self.assertEqual(sts.assume_role.call_args.kwargs["RoleArn"], "arn:aws-us-gov:iam::222222222222:role/audit/EBSInventoryRole")
        self.assertEqual(sts.assume_role.call_args.kwargs["ExternalId"], "audit-id")


if __name__ == "__main__":
    unittest.main()
