# Remote state for the main stack.
#
# The bucket is created by infra/bootstrap, which must be applied first.
# `terraform init` here will fail until that has happened — that is expected,
# not a misconfiguration.
#
# Backend blocks cannot use variables or locals, so the bucket name is
# hardcoded. It must stay in sync with the `state_bucket_name` output of the
# bootstrap stack.
#
# use_lockfile = true enables S3-native state locking (Terraform >= 1.10),
# which replaces the DynamoDB lock table the old docs tell you to create.

terraform {
  required_version = ">= 1.11.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }

  backend "s3" {
    bucket       = "ai-pre-traffic-gate-tfstate-594380318102"
    key          = "personal/terraform.tfstate"
    region       = "us-east-1"
    encrypt      = true
    use_lockfile = true
  }
}
