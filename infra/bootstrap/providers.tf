# Bootstrap stack.
#
# This is the ONE stack that uses local state, because it creates the S3 bucket
# that every other stack stores its state in. Classic chicken-and-egg: you
# cannot store your state in a bucket that does not exist yet.
#
# It also creates the account-wide cost budget, so the spend guardrail exists
# before any billable resource does.
#
# Apply this once, from an admin profile. After that it should rarely change.

terraform {
  required_version = ">= 1.11.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
}

provider "aws" {
  region = var.aws_region

  # Guardrail: fail loudly if pointed at the wrong account. Cheap insurance
  # against applying a personal-account stack into a company account.
  allowed_account_ids = [var.aws_account_id]

  default_tags {
    tags = {
      Project   = "ai-pre-traffic-gate"
      ManagedBy = "terraform"
      Stack     = "bootstrap"
      Teardown  = "true"
    }
  }
}
