output "firewall_rule_name" {
  description = "Name of the managed firewall rule."
  value       = google_compute_firewall.allow_iap_ssh.name
}

output "firewall_rule_self_link" {
  description = "Self-link of the managed firewall rule."
  value       = google_compute_firewall.allow_iap_ssh.self_link
}

output "source_ranges" {
  description = "Source ranges the rule admits. Should only ever be the IAP TCP-forwarding range."
  value       = google_compute_firewall.allow_iap_ssh.source_ranges
}

output "scoped_by" {
  description = "Whether the rule is scoped by service account (preferred) or network tag."
  value       = local.use_service_accounts ? "target_service_accounts" : "target_tags"
}

output "logging_enabled" {
  description = "Whether Firewall Rules Logging is enabled on the rule."
  value       = var.enable_logging
}

output "import_command" {
  description = "Command that binds the pre-existing live rule to this configuration."
  value       = "terraform import google_compute_firewall.allow_iap_ssh projects/${var.project_id}/global/firewalls/${var.rule_name}"
}
