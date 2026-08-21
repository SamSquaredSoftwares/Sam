variable "project_id" {
  description = "GCP project that owns the VPC and the firewall rule."
  type        = string
  default     = "sam-squared-samepos-prod"
}

variable "network" {
  description = "Name (or self-link) of the VPC the rule attaches to."
  type        = string
  default     = "samepos-vpc"
}

variable "rule_name" {
  description = "Name of the firewall rule. Must match the live rule for `terraform import` to bind to it."
  type        = string
  default     = "samepos-allow-iap-ssh"
}

variable "description" {
  description = "Rule description. Defaults to \"\" so the config mirrors the live rule on import; see step 2 in the README."
  type        = string
  default     = ""
}

variable "priority" {
  description = "Rule priority. Lower numbers evaluate first; the implied deny sits at 65535."
  type        = number
  default     = 1000

  validation {
    condition     = var.priority >= 0 && var.priority <= 65535
    error_message = "priority must be between 0 and 65535."
  }
}

variable "ssh_port" {
  description = "TCP port sshd listens on for the target instances."
  type        = string
  default     = "22"
}

variable "target_tags" {
  description = "Network tags the rule applies to. Ignored when target_service_accounts is non-empty."
  type        = list(string)
  default     = ["samepos-backend"]
}

variable "target_service_accounts" {
  description = <<-EOT
    Service accounts the rule applies to. Preferred over target_tags: a network tag
    can be attached to any instance by a principal holding compute.instances.setTags,
    which quietly pulls that instance into scope of this rule. Changing an instance's
    service account requires stopping it plus iam.serviceAccounts.actAs, so it is not
    self-assignable. Setting this to a non-empty list makes target_tags inert.
  EOT
  type        = list(string)
  default     = []
}

variable "enable_logging" {
  description = "Enable Firewall Rules Logging. Defaults to false so the config mirrors the live rule on import; see step 2 in the README."
  type        = bool
  default     = false
}

variable "log_metadata" {
  description = "Metadata attached to each log entry. EXCLUDE_ALL_METADATA keeps log volume and Cloud Logging cost down."
  type        = string
  default     = "EXCLUDE_ALL_METADATA"

  validation {
    condition     = contains(["INCLUDE_ALL_METADATA", "EXCLUDE_ALL_METADATA"], var.log_metadata)
    error_message = "log_metadata must be INCLUDE_ALL_METADATA or EXCLUDE_ALL_METADATA."
  }
}
