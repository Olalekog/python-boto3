# Organization-wide unattached EBS audit

This Python/Boto3 script discovers **currently unattached EBS volumes created
more than 30 days ago**, estimates their monthly cost, and optionally produces
a deletion proposal for review. It never deletes volumes or creates snapshots.

## What it does

- Paginates AWS Organizations accounts, using the current `State` field, and scans ACTIVE accounts.
- Assumes a named read-only IAM role in every account, including the management account by default.
- Discovers enabled EC2 Regions separately for each account, including opted-in Regions.
- Paginates available EBS volumes, including AWS-managed resources, and also checks zero attachments.
- Applies a strict UTC creation-time cutoff: exactly 30 days old is excluded.
- Looks up public On-Demand USD regional pricing using the AWS Price List Query API.
- Includes provisioned storage, gp3 performance above its baseline, io1 IOPS, and io2 tiered IOPS.
- Retains protected volumes in the cost report but excludes them from the optional plan.
- Records failed account/Region scans and missing prices instead of silently omitting failures.

## Age interpretation

`CreateTime` is **volume creation time**, not last-detachment time. A 90-day-old
volume detached yesterday will match this scan. The report and plan explicitly
label this age basis. This tool does not claim that a volume has been unattached
continuously for 30 days. For that policy, add an authoritative history source
from AWS Config or retained CloudTrail events and verify intervening attachments.
Do not infer duration from a single `DetachVolume` event alone.

## Requirements and permissions

- Python 3.11 or newer, Boto3, and valid AWS credentials.
- Run from the Organization management account or a delegated administrator
  permitted to call `organizations:ListAccounts`.
- Deploy `EBSInventoryRole` with `scanner-role-policy.json` and
  `scanner-role-trust.json` in each account you want to scan. Organizations does
  not create cross-account access automatically. StackSets can distribute this role.
- In the trust document, replace account `111111111111` and the runner role
  name with your actual **IAM role ARN**, not an STS assumed-role session ARN.
- Attach `runner-policy.json` to the runner. Restrict its AssumeRole resource
  to your organization's explicit account IDs if possible. Match any role path
  used by `--role-name` in the policy ARN.
- SCPs, permissions boundaries, and trust policies must also permit access.
- If using `--external-id`, add the matching `sts:ExternalId` condition to the
  target roles' trust policies. The value is applied to every assumed role.
- Using `--use-current-account-credentials` requires the runner to also have
  `scanner-role-policy.json` permissions for its own account.

The provided policies grant no DeleteVolume, CreateSnapshot, or other write
permissions. Deploy these policies yourself through your normal IAM workflow.
No IAM resources are created by the script.

## Install and authenticate

```bash
python -m venv .venv
# Linux/macOS:
source .venv/bin/activate
# Windows PowerShell alternative:
# .\.venv\Scripts\Activate.ps1

python -m pip install -r requirements.txt
aws configure sso --profile org-audit
aws sso login --profile org-audit
```

You can also omit `--profile` and use Boto3's default credential chain, including
the IAM role attached to your execution environment. Never embed access keys.

## Run a full report

```bash
python ebs_audit.py \
  --profile org-audit \
  --role-name EBSInventoryRole \
  --days 30 \
  --output-dir reports/full-scan
```

PowerShell: put the command on one line, or use PowerShell backticks instead
of bash continuation backslashes.

## Generate the optional deletion proposal

```bash
python ebs_audit.py \
  --profile org-audit \
  --role-name EBSInventoryRole \
  --days 30 \
  --deletion-plan \
  --plan-require-tag CleanupApproved=true \
  --output-dir reports/review-scan
```

`--plan-require-tag` restricts only plan eligibility, not reporting. Omit it to
propose all otherwise eligible candidates. Key/value matching for this flag is
case sensitive.

Protection tag keys `DoNotDelete`, `Keep`, and `Retention` are always respected
case insensitively by key presence, regardless of value: `Keep=false` is still
protected. Add your own keys with repeated `--protect-tag-key` arguments.
AWS-managed volumes and Outposts volumes are excluded from the plan. These
filters do not replace owner review or evidence of retention requirements.

## Restrict scope while validating access

```bash
python ebs_audit.py \
  --profile org-audit \
  --accounts 222222222222 333333333333 \
  --regions us-east-1 us-west-2 \
  --output-dir reports/pilot
```

An account ID outside the Organization is an error. A requested Region that
is unknown or disabled in an account is reported as incomplete coverage.
Choose a new output directory for every run; existing report files are not
overwritten. This also prevents a prior plan from surviving a newer report-only run.

## Output files

| File | Contents |
| --- | --- |
| `volumes.csv` | One candidate per row, account, Region, volume, size, age, tags, eligibility, estimated costs and price errors |
| `report.json` | Complete candidate list, totals by account/Region, coverage, errors, pricing evidence and assumptions |
| `deletion-plan.json` | Optional review-only proposal, exact account/Region/volume identifiers, current tag evidence and required preconditions |

The terminal prints an overall JSON summary. `inventory_complete` describes
coverage **within the selected ACTIVE-account/enabled-Region scope**. Inactive
accounts are explicitly listed as skipped. Pricing completeness is a separate
field. Totals named `known_*` include only the costs that could be estimated;
unknown costs are null, not zero. Protected resources count toward overall
costs but not proposed savings.

Exit status:

- `0`: scan complete in the selected scope and all discovered candidates priced.
- `2`: report is partial, AWS access/discovery failed, a price is missing, or CLI input is invalid.
- Other nonzero status: unexpected execution or local output failure.

Account/Region failures are in `report.json.errors`; individual price failures
are in each volume's `cost_error`. Check both before presenting totals as an
Organization-wide estimate. A partial scan still writes available results and
an optional plan marked with its inventory completeness.

## Cost calculations

For an entire month at unchanged configuration:

```text
Storage = provisioned Size × regional USD/GB-month price
gp3 IOPS = max(IOPS - 3,000, 0) × regional USD/IOPS-month price
gp3 throughput = max(Throughput_MiB_s - 125, 0) / 1,024
                 × regional USD/GiBps-month price
io1 IOPS = provisioned IOPS × regional USD/IOPS-month price
io2 IOPS = first 32,000 × tier-1 price
           + next 32,000 × tier-2 price
           + remaining IOPS × tier-3 price
Monthly estimate = storage + provisioned performance
```

Storage capacity from EC2 is in GiB; AWS EBS storage pricing uses the GB-month
dimension for this provisioned capacity. The Price List's gp3 throughput
dimension uses GiBps-month; EC2 returns MiB/s, hence the explicit conversion.
Prices are cached per Region and component and include SKU/effective-date
evidence in the JSON report. No price values are hardcoded.

Estimates are **current monthly run rates**, not actual invoice amounts or
historical spending over a volume's entire creation age. Taxes, discounts,
credits, snapshots, and magnetic-volume historical I/O are excluded.
Local/Wavelength Zones and Outposts need separate pricing and are reported as
unknown; they are never priced using a parent Region rate. AWS commercial
Regions are the primary supported environment. Role ARNs honor the caller's
partition; GovCloud/China pricing coverage, credentials and endpoint support
must be validated separately. A missing/ambiguous/non-flat price becomes an
explicit partial or unknown estimate rather than a guessed rate.

Scans are sequential to control API load. Standard SDK retries handle transient
failures, but this is not an atomic inventory snapshot: state can change during
the scan. Each account gets a one-hour STS session; an unusually long scan may
need to be rerun with narrower account/Region scope. There is no credential
refresh or checkpoint resume within an account.

## Reviewing the deletion plan

The JSON contains a proposed API operation and parameters, not executable shell
commands. Before implementing a separate executor:

1. Confirm account and Region identity and obtain owner/change approval.
2. Check attachment history and retention needs; creation age alone is insufficient.
3. Snapshot volumes where required and wait for snapshot completion.
4. Immediately re-describe each volume and require available state, no
   attachments, the age cutoff, valid tags and unmanaged/non-Outposts status.
5. Perform deletion through a separately authorized role, with an audit log.

The script contains no apply flag and makes no deletion calls.

## Validation

```bash
python -m unittest -v test_ebs_audit.py
```

The package was verified with 14 offline tests, including real botocore Stubber
checks for EC2, Organizations, and pricing pagination. Tests cover age boundaries,
protected/managed volumes, gp3 unit conversion, io2 tiers, missing/ambiguous prices,
cross-account ARNs and partial-result reporting. It has not been run against
your AWS accounts; begin with a restricted pilot scan.

## AWS references

- [Organizations ListAccounts and account State](https://docs.aws.amazon.com/organizations/latest/APIReference/API_ListAccounts.html)
- [EC2 DescribeVolumes, creation time, and managed resources](https://docs.aws.amazon.com/AWSEC2/latest/APIReference/API_DescribeVolumes.html)
- [EC2 DescribeRegions](https://docs.aws.amazon.com/boto3/latest/reference/services/ec2/client/describe_regions.html)
- [AWS Price List Query API](https://docs.aws.amazon.com/awsaccountbilling/latest/aboutv2/using-price-list-query-api.html)
- [EBS pricing and tier examples](https://aws.amazon.com/ebs/pricing/)
- [EBS billing usage types](https://docs.aws.amazon.com/ebs/latest/userguide/ebs-billing-usage-reports.html)
