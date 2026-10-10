# Terraform — GCP network baseline

Manages the IAP-only SSH ingress path for the `samepos-vpc` network in
`sam-squared-samepos-prod`.

## What this manages

One rule: `samepos-allow-iap-ssh`. It admits `tcp:22` from `35.235.240.0/20`
— Google's IAP TCP-forwarding range — to instances scoped by network tag
(`samepos-backend`) or, preferably, by service account.

Because the range is fixed by Google and not internet-routable, this is the
entire public-SSH story for the VPC: with no broader `:22` rule present, the
implied deny at priority 65535 closes everything else.

## The rule already exists — import before you apply

The live rule was created out of band (console/CLI) on 2026-08-17. Applying this
config without importing first will fail with `alreadyExists`, so bind the
existing resource to state first.

The defaults in `variables.tf` deliberately mirror the live rule *exactly*
(`description = ""`, `enable_logging = false`), so a clean no-change plan is
your proof that the config is a faithful description of reality before you
change anything.

### Step 1 — import and confirm zero drift

```bash
terraform -chdir=infra/terraform init
```

```bash
terraform -chdir=infra/terraform import google_compute_firewall.allow_iap_ssh projects/sam-squared-samepos-prod/global/firewalls/samepos-allow-iap-ssh
```

```bash
terraform -chdir=infra/terraform plan
```

That plan must report **"No changes."** If it does not, the config and the live
rule disagree — reconcile before going further rather than letting an apply
paper over the difference.

On Terraform 1.5+ you can use a config-driven `import` block instead of the CLI
command, then delete the block after the first successful apply:

```hcl
import {
  to = google_compute_firewall.allow_iap_ssh
  id = "projects/sam-squared-samepos-prod/global/firewalls/samepos-allow-iap-ssh"
}
```

### Step 2 — apply the hardening as its own change

Only once step 1 is clean, turn on the two improvements:

```hcl
description    = "Allow SSH from the Google IAP TCP-forwarding range only; no public SSH ingress."
enable_logging = true
```

The resulting plan is a single in-place update, with no traffic interruption:
firewall rule updates do not drop established connections.

## Scoping: tags vs service accounts

`target_tags` is what the live rule uses and what this config defaults to, but
it is the weaker control. Any principal holding `compute.instances.setTags` can
attach `samepos-backend` to an arbitrary instance and pull it into scope of this
rule. A service account cannot be self-assigned that way — changing an
instance's SA requires stopping it plus `iam.serviceAccounts.actAs`.

To switch, empty the tags and set the SA:

```hcl
target_tags             = []
target_service_accounts = ["samepos-backend@sam-squared-samepos-prod.iam.gserviceaccount.com"]
```

The two are mutually exclusive in the GCP API; when both are set here, the
service account list wins. A `precondition` blocks the both-empty case, which
GCP would otherwise interpret as "every instance in the VPC".

## What this does *not* cover

The firewall rule is necessary but not sufficient for IAP SSH, and it is not the
access control. Principals still need:

- `roles/iap.tunnelResourceAccessor` on the project or instance
- `roles/compute.osLogin` or `roles/compute.osAdminLogin`

Since the firewall is now the only network-layer control, audit those bindings —
especially for `allAuthenticatedUsers` or broad groups:

```bash
gcloud projects get-iam-policy sam-squared-samepos-prod --flatten="bindings[].members" --filter="bindings.role:roles/iap.tunnelResourceAccessor" --format="table(bindings.role,bindings.members)"
```

Also confirm no other rule opens `:22` more broadly, which would make this rule
decorative. Do **not** filter on the port — list every ingress rule and read the
allow column yourself:

```bash
gcloud compute firewall-rules list --project=sam-squared-samepos-prod --filter="network~samepos-vpc AND direction=INGRESS AND NOT disabled" --format="table(name,priority,sourceRanges.list(),allowed[].map().firewall_rule().list(),targetTags.list(),targetServiceAccounts.list())"
```

Then eyeball every row whose source range is not `35.235.240.0/20`. A port
predicate such as `allowed[].ports~22` is actively misleading here and must not
be used: a rule created with `--allow=tcp` or `--allow=all` carries no `ports`
field at all, and a range like `0-65535` or `20-30` contains 22 without
containing the literal string — so the rules that would defeat this control are
exactly the ones such a filter cannot match. It returns an empty table, which
reads as an all-clear.

## State

State is local by default, which is wrong for a production firewall — it holds
the VPC's ingress posture and is the only drift record. Create a versioned GCS
bucket, uncomment the `backend "gcs"` block in `versions.tf`, and run
`terraform init -migrate-state`.

Commit `.terraform.lock.hcl` so provider versions are reproducible; `.gitignore`
already excludes state, `.terraform/`, and real `*.tfvars`.
