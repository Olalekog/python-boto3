#!/usr/bin/env python3
"""Read-only AWS Organizations EBS inventory, cost estimates and review plan.

Age means creation age, not continuous detachment duration. No deletion API
is called. See README.md for IAM deployment and pricing assumptions.
"""
import argparse
import csv
import json
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

CONFIG = Config(retries={"mode": "standard", "max_attempts": 8},
                connect_timeout=10, read_timeout=60)
AWS_ERRORS = (BotoCoreError, ClientError)
SUFFIX = {"gp2": ".gp2", "gp3": ".gp3", "io1": ".piops",
          "io2": ".io2", "st1": ".st1", "sc1": ".sc1", "standard": ""}
FIELDS = ["account_id", "account_name", "region", "availability_zone",
          "volume_id", "volume_type", "size_gib", "iops", "throughput_mib_s",
          "created_at", "creation_age_days", "encrypted", "managed",
          "plan_eligible", "exclusion_reason", "storage_monthly_usd",
          "performance_monthly_usd", "estimated_monthly_usd",
          "cost_status", "cost_error", "tags"]


def money(value):
    return str(value.quantize(Decimal("0.000001"))) if value is not None else None


def old_unattached(volume, now, days):
    return (volume.get("State") == "available" and not volume.get("Attachments")
            and volume["CreateTime"] < now - timedelta(days=days))


def plan_exclusions(volume, protection_keys, required_tag):
    tags = {t["Key"]: t["Value"] for t in volume.get("Tags", [])}
    reasons = []
    # Presence alone protects a volume, even when its tag value says "false".
    if any(key.casefold() in {k.casefold() for k in tags} for key in protection_keys):
        reasons.append("protection_tag_present")
    if volume.get("Operator", {}).get("Managed"):
        reasons.append("aws_managed_volume")
    if volume.get("OutpostArn"):
        reasons.append("outposts_requires_separate_review")
    if required_tag:
        key, value = required_tag.split("=", 1)
        if tags.get(key) != value:
            reasons.append("required_plan_tag_missing")
    return reasons


class Prices:
    """Cached public On-Demand USD prices; never substitute zero for missing prices."""
    def __init__(self, client):
        self.client = client
        self.cache = {}
        self.evidence = {}

    def lookup(self, region, field, value, expected_unit):
        key = (region, field, value, expected_unit)
        if key in self.cache:
            result = self.cache[key]
            if isinstance(result, Exception):
                raise result
            return result
        try:
            candidates = []
            pages = self.client.get_paginator("get_products").paginate(
                ServiceCode="AmazonEC2", FormatVersion="aws_v1",
                Filters=[{"Type": "TERM_MATCH", "Field": "regionCode", "Value": region},
                         {"Type": "TERM_MATCH", "Field": field, "Value": value}])
            for page in pages:
                for raw in page.get("PriceList", []):
                    product = json.loads(raw)
                    attrs = product["product"]["attributes"]
                    # Exclude Outposts, Local Zones, snapshots and unrelated products.
                    usage = attrs.get("usagetype", "")
                    if attrs.get("locationType") != "AWS Region":
                        continue
                    if field == "volumeApiName":
                        suffix = "EBS:VolumeUsage" + SUFFIX[value]
                        if not (usage == suffix or usage.endswith("-" + suffix)):
                            continue
                    for term in product.get("terms", {}).get("OnDemand", {}).values():
                        for dimension in term.get("priceDimensions", {}).values():
                            if dimension.get("unit") != expected_unit:
                                continue
                            # Each queried usage type must have a single flat dimension.
                            if dimension.get("beginRange") != "0" or dimension.get("endRange") != "Inf":
                                continue
                            rate = dimension.get("pricePerUnit", {}).get("USD")
                            if rate is not None:
                                candidates.append((Decimal(rate), usage,
                                                   product["product"]["sku"],
                                                   term.get("effectiveDate", "")))
            if not candidates:
                raise ValueError(f"No flat USD {expected_unit} price for {region}/{value}")
            latest = max(c[3] for c in candidates)
            candidates = [c for c in candidates if c[3] == latest]
            if len({(c[0], c[1]) for c in candidates}) != 1:
                raise ValueError(f"Ambiguous price for {region}/{value}")
            rate, usage, sku, effective = candidates[0]
            result = (rate, usage)
            self.cache[key] = result
            self.evidence["|".join(key)] = {"rate_usd": str(rate), "unit": expected_unit,
                                           "usage_type": usage, "sku": sku,
                                           "effective_date": effective}
            return result
        except (*AWS_ERRORS, ValueError) as exc:
            self.cache[key] = exc
            raise

    def estimate(self, region, volume):
        if volume.get("OutpostArn"):
            return None, None, None, "unknown", "Outposts pricing is not supported"
        if not re.fullmatch(re.escape(region) + r"[a-z]", volume["AvailabilityZone"]):
            return None, None, None, "unknown", "Local/Wavelength Zone pricing requires separate review"
        kind = volume["VolumeType"]
        storage = None
        try:
            if kind not in SUFFIX:
                raise ValueError(f"Unsupported volume type {kind}")
            rate, usage = self.lookup(region, "volumeApiName", kind, "GB-Mo")
            storage = rate * Decimal(volume["Size"])
            prefix = usage[:-len("EBS:VolumeUsage" + SUFFIX[kind])]

            def performance_rate(code, unit):
                return self.lookup(region, "usagetype", prefix + code, unit)[0]

            performance = Decimal(0)
            if kind == "gp3":
                extra_iops = max(0, volume["Iops"] - 3000)
                extra_throughput = max(0, volume["Throughput"] - 125)
                if extra_iops:
                    performance += extra_iops * performance_rate("EBS:VolumeP-IOPS.gp3", "IOPS-Mo")
                if extra_throughput:
                    # EC2 returns MiB/s; this Price List dimension is GiB/s-month.
                    performance += Decimal(extra_throughput) / 1024 * performance_rate("EBS:VolumeP-Throughput.gp3", "GiBps-mo")
            elif kind == "io1":
                performance = volume["Iops"] * performance_rate("EBS:VolumeP-IOPS.piops", "IOPS-Mo")
            elif kind == "io2":
                iops = volume["Iops"]
                for count, suffix in [(min(iops, 32000), ""),
                                      (min(max(iops - 32000, 0), 32000), ".tier2"),
                                      (max(iops - 64000, 0), ".tier3")]:
                    if count:
                        performance += count * performance_rate("EBS:VolumeP-IOPS.io2" + suffix, "IOPS-Mo")
            return storage, performance, storage + performance, "estimated", ""
        except (*AWS_ERRORS, ValueError, KeyError) as exc:
            return storage, None, None, "partial" if storage is not None else "unknown", str(exc)


def discover_accounts(org, wanted):
    accounts = []
    for page in org.get_paginator("list_accounts").paginate():
        accounts.extend(page.get("Accounts", []))
    known = {a["Id"] for a in accounts}
    if wanted - known:
        raise ValueError("Account IDs not in Organizations: " + ", ".join(sorted(wanted - known)))
    selected = [a for a in accounts if not wanted or a["Id"] in wanted]
    if any("State" not in a for a in selected):
        raise ValueError("Organizations response lacks State; update boto3/botocore")
    active = [a for a in selected if a.get("State") == "ACTIVE"]
    skipped = [{"account_id": a["Id"], "state": a.get("State", "UNKNOWN")}
               for a in selected if a.get("State") != "ACTIVE"]
    return active, skipped


def account_session(base, sts, account_id, caller_id, partition, args):
    if account_id == caller_id and args.use_current_account_credentials:
        return base
    kwargs = {"RoleArn": f"arn:{partition}:iam::{account_id}:role/{args.role_name}",
              "RoleSessionName": "ebs-unattached-audit", "DurationSeconds": 3600}
    if args.external_id:
        kwargs["ExternalId"] = args.external_id
    credentials = sts.assume_role(**kwargs)["Credentials"]
    return boto3.Session(aws_access_key_id=credentials["AccessKeyId"],
                         aws_secret_access_key=credentials["SecretAccessKey"],
                         aws_session_token=credentials["SessionToken"],
                         region_name=args.home_region)


def scan_region(ec2, account, region, now, args, prices, rows):
    count = 0
    for page in ec2.get_paginator("describe_volumes").paginate(
            Filters=[{"Name": "status", "Values": ["available"]}],
            IncludeManagedResources=True):
        for volume in page.get("Volumes", []):
            if not old_unattached(volume, now, args.days):
                continue
            reasons = plan_exclusions(volume, args.protect_tag_key, args.plan_require_tag)
            storage, performance, total, status, error = prices.estimate(region, volume)
            rows.append({"account_id": account["Id"], "account_name": account["Name"],
                         "region": region, "availability_zone": volume["AvailabilityZone"],
                         "volume_id": volume["VolumeId"], "volume_type": volume["VolumeType"],
                         "size_gib": volume["Size"], "iops": volume.get("Iops"),
                         "throughput_mib_s": volume.get("Throughput"),
                         "created_at": volume["CreateTime"].isoformat(),
                         "creation_age_days": round((now - volume["CreateTime"]).total_seconds() / 86400, 3),
                         "encrypted": volume.get("Encrypted", False),
                         "managed": volume.get("Operator", {}).get("Managed", False),
                         "plan_eligible": not reasons, "exclusion_reason": ";".join(reasons),
                         "storage_monthly_usd": money(storage),
                         "performance_monthly_usd": money(performance),
                         "estimated_monthly_usd": money(total), "cost_status": status,
                         "cost_error": error,
                         "tags": {t["Key"]: t["Value"] for t in volume.get("Tags", [])}})
            count += 1
    return count


def summarize(rows):
    def group(items):
        priced = [r for r in items if r["estimated_monthly_usd"] is not None]
        eligible = [r for r in priced if r["plan_eligible"]]
        return {"volume_count": len(items), "size_gib": sum(r["size_gib"] for r in items),
                "fully_priced_count": len(priced), "unpriced_count": len(items) - len(priced),
                "plan_eligible_count": sum(r["plan_eligible"] for r in items),
                "known_estimated_monthly_usd": money(sum((Decimal(r["estimated_monthly_usd"]) for r in priced), Decimal(0))),
                "known_plan_eligible_monthly_usd": money(sum((Decimal(r["estimated_monthly_usd"]) for r in eligible), Decimal(0))),
                "known_storage_monthly_usd": money(sum((Decimal(r["storage_monthly_usd"]) for r in items if r["storage_monthly_usd"] is not None), Decimal(0)))}
    groups = defaultdict(list)
    for row in rows:
        groups[(row["account_id"], row["region"])].append(row)
    return {"overall": group(rows), "by_account_region": [
        {"account_id": account, "region": region, **group(items)}
        for (account, region), items in sorted(groups.items())]}


def deletion_plan(rows, now, args):
    return {"schema_version": 1, "generated_at": now.isoformat(),
            "mode": "review_only", "age_basis": "volume_creation_time",
            "minimum_creation_age_days": args.days,
            "warning": "Creation age does not prove 30 days of continuous detachment. No commands are executed.",
            "required_preconditions": [
                "Verify the target account and Region, and obtain owner/change approval.",
                "Confirm recent attach/detach history using retained CloudTrail or AWS Config records.",
                "Re-describe immediately before deletion; require available state and zero attachments.",
                "Recheck creation time, AWS-managed status, Outposts status and protection/required tags.",
                "Review retention requirements; create and wait for a completed snapshot when required.",
                "Use a separately authorized deletion role; this scanner role is read-only."],
            "items": [{"account_id": r["account_id"], "region": r["region"],
                       "volume_id": r["volume_id"], "created_at": r["created_at"],
                       "tags_at_scan": r["tags"], "estimated_monthly_usd": r["estimated_monthly_usd"],
                       "proposed_api": "ec2.DeleteVolume",
                       "parameters": {"VolumeId": r["volume_id"]},
                       "review_status": "pending_owner_review"}
                      for r in rows if r["plan_eligible"]]}


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--profile", help="Organizations/pricing/STS caller profile")
    p.add_argument("--home-region", default="us-east-1")
    p.add_argument("--pricing-region", default="us-east-1")
    p.add_argument("--role-name", default="EBSInventoryRole", help="Existing IAM role name or path in each account")
    p.add_argument("--external-id")
    p.add_argument("--use-current-account-credentials", action="store_true",
                   help="Only for the source account; other accounts still use AssumeRole")
    p.add_argument("--days", type=int, default=30)
    p.add_argument("--accounts", nargs="+", help="Optional account ID restriction")
    p.add_argument("--regions", nargs="+", help="Optional Region restriction")
    p.add_argument("--protect-tag-key", action="append",
                   help="Additional protection tag key; defaults always remain active")
    p.add_argument("--plan-require-tag", help="Restrict plan candidates to KEY=VALUE (case sensitive)")
    p.add_argument("--deletion-plan", action="store_true", help="Write review-only JSON plan")
    p.add_argument("--output-dir", default="ebs-report")
    return p


def main(argv=None):
    p = parser()
    args = p.parse_args(argv)
    if args.days < 1:
        p.error("--days must be positive")
    if args.plan_require_tag and ("=" not in args.plan_require_tag or not args.plan_require_tag.split("=", 1)[0]):
        p.error("--plan-require-tag must be KEY=VALUE")
    if args.accounts and any(len(a) != 12 or not a.isdigit() for a in args.accounts):
        p.error("--accounts requires 12-digit IDs")
    args.protect_tag_key = ["DoNotDelete", "Keep", "Retention"] + (args.protect_tag_key or [])
    out = Path(args.output_dir)
    # Prevent stale plans/reports from a previous scan being mistaken for new output.
    out.mkdir(parents=True, exist_ok=True)
    if any((out / n).exists() for n in ["volumes.csv", "report.json", "deletion-plan.json"]):
        p.error("Output files already exist; choose a new --output-dir")
    now = datetime.now(timezone.utc)
    rows, errors, coverage, accounts, skipped = [], [], [], [], []
    price_client = None
    prices = None
    try:
        base = boto3.Session(profile_name=args.profile, region_name=args.home_region)
        sts = base.client("sts", config=CONFIG)
        identity = sts.get_caller_identity()
        partition = identity["Arn"].split(":")[1]
        org = base.client("organizations", config=CONFIG)
        accounts, skipped = discover_accounts(org, set(args.accounts or []))
        price_client = base.client("pricing", region_name=args.pricing_region, config=CONFIG)
        prices = Prices(price_client)
        for account in accounts:
            print(f"Scanning {account['Id']} ({account['Name']})", file=sys.stderr)
            try:
                session = account_session(base, sts, account["Id"], identity["Account"], partition, args)
                bootstrap = session.client("ec2", region_name=args.home_region, config=CONFIG)
                enabled = {r["RegionName"] for r in bootstrap.describe_regions(AllRegions=True)["Regions"]
                           if r["OptInStatus"] in ("opt-in-not-required", "opted-in")}
                bootstrap.close()
            except AWS_ERRORS as exc:
                errors.append({"stage": "account_access_or_region_discovery", "account_id": account["Id"], "error": str(exc)})
                continue
            requested = set(args.regions or enabled)
            for region in sorted(requested - enabled):
                errors.append({"stage": "region_not_enabled_or_unknown", "account_id": account["Id"], "region": region})
            for region in sorted(requested & enabled):
                client = session.client("ec2", region_name=region, config=CONFIG)
                try:
                    count = scan_region(client, account, region, now, args, prices, rows)
                    coverage.append({"account_id": account["Id"], "region": region, "status": "complete", "candidate_count": count})
                except AWS_ERRORS as exc:
                    errors.append({"stage": "volume_scan", "account_id": account["Id"], "region": region, "error": str(exc)})
                    coverage.append({"account_id": account["Id"], "region": region, "status": "partial_or_failed"})
                finally:
                    client.close()
    except (*AWS_ERRORS, ValueError) as exc:
        errors.append({"stage": "initialization_or_account_discovery", "error": str(exc)})
    finally:
        if price_client is not None:
            price_client.close()
    rows.sort(key=lambda r: (r["account_id"], r["region"], r["volume_id"]))
    cost_failures = sum(r["cost_status"] != "estimated" for r in rows)
    report = {"schema_version": 1, "generated_at": now.isoformat(),
              "age_basis": "volume_creation_time", "older_than_days": args.days,
              "scope": {"accounts": args.accounts or "all ACTIVE Organization accounts",
                        "regions": args.regions or "all enabled EC2 Regions per account"},
              "inventory_complete": not errors,
              "pricing_complete_for_discovered_candidates": not cost_failures,
              "cost_basis": "Public On-Demand USD monthly run rate, not billed historical spend",
              "cost_exclusions": ["tax", "discounts", "credits", "snapshot storage",
                                  "historical magnetic-volume I/O", "Outposts and Local Zone-specific pricing"],
              "active_accounts_selected": [a["Id"] for a in accounts],
              "skipped_inactive_accounts": skipped, "coverage": coverage,
              "errors": errors, "summary": summarize(rows), "volumes": rows,
              "price_evidence": prices.evidence if prices else {}}
    with (out / "volumes.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({**row, "tags": json.dumps(row["tags"], sort_keys=True)})
    (out / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    if args.deletion_plan:
        plan = deletion_plan(rows, now, args)
        plan["inventory_complete"] = not errors
        plan["protection_tag_keys"] = args.protect_tag_key
        plan["required_tag"] = args.plan_require_tag
        (out / "deletion-plan.json").write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({**report["summary"]["overall"], "inventory_complete": not errors,
                      "cost_failures": cost_failures, "output_dir": str(out)}, indent=2))
    # Code 2 means usable partial output, not a clean organization-wide result.
    return 2 if errors or cost_failures else 0


if __name__ == "__main__":
    sys.exit(main())
