provider "aws" {
  region = var.aws_region

  allowed_account_ids = [var.aws_account_id]

  default_tags {
    tags = {
      Project   = "ai-pre-traffic-gate"
      ManagedBy = "terraform"
      Stack     = "personal"
      Teardown  = "true"
    }
  }
}
