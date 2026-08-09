# Main stack for the personal account.
#
# Intentionally near-empty at Phase 0. This exists so the remote backend, the
# provider pin, the account guard and the default tags are all proven before
# any real resource depends on them.
#
# Phase 1 adds: demo app Lambda + alias, CodeDeploy deployment group,
# CodeBuild project, CodePipeline, the artifact bucket, and the hardcoded
# halt stub.

data "aws_caller_identity" "current" {}

data "aws_region" "current" {}
