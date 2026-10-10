terraform {
  required_version = ">= 1.5.0"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 5.0, < 8.0"
    }
  }

  # State for a production firewall should not live on a laptop: it holds the
  # full ingress posture of the VPC and is the only record of drift. Create a
  # versioned GCS bucket, then uncomment and run `terraform init -migrate-state`.
  #
  # backend "gcs" {
  #   bucket = "sam-squared-samepos-prod-tfstate"
  #   prefix = "network/firewall"
  # }
}

provider "google" {
  project = var.project_id
}
