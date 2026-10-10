locals {
  # Google's fixed source range for IAP TCP forwarding. Every IAP tunnel egresses
  # from here and nothing else does, and the range is not internet-routable — that
  # is what makes this rule an IAP-only SSH path rather than a public one.
  # https://cloud.google.com/iap/docs/using-tcp-forwarding
  iap_tcp_forwarding_range = "35.235.240.0/20"

  # Service accounts win when both are supplied; see variables.tf for why they
  # are the stronger scoping mechanism.
  use_service_accounts = length(var.target_service_accounts) > 0
}

resource "google_compute_firewall" "allow_iap_ssh" {
  project     = var.project_id
  name        = var.rule_name
  network     = var.network
  description = var.description

  direction = "INGRESS"
  priority  = var.priority
  disabled  = false

  # SSH reaches these instances only through an IAP tunnel. Adding anything to
  # this list re-opens the public SSH path the rule exists to close.
  source_ranges = [local.iap_tcp_forwarding_range]

  allow {
    protocol = "tcp"
    ports    = [var.ssh_port]
  }

  target_tags             = local.use_service_accounts ? null : var.target_tags
  target_service_accounts = local.use_service_accounts ? var.target_service_accounts : null

  # Absent block == logging disabled, which is the live state. Flipping
  # enable_logging materialises the block and turns it on.
  dynamic "log_config" {
    for_each = var.enable_logging ? [1] : []

    content {
      metadata = var.log_metadata
    }
  }

  lifecycle {
    precondition {
      condition     = length(var.target_tags) > 0 || length(var.target_service_accounts) > 0
      error_message = "Set target_tags or target_service_accounts. With both empty, GCP applies the rule to every instance in the VPC, opening port 22 far wider than intended."
    }
  }
}
