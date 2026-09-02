# Phase 2.5 -- synthesized-real signal generation.
#
# CLAUDE.md distinguishes MOCKED signals (hardcoded JSON, unblocks development,
# never touches AWS) from SYNTHESIZED-REAL ones (conditions engineered in the
# account so real AWS services genuinely emit real findings). Both are required:
# mocked signals alone hide parsing bugs and produce a demo that cannot be
# honestly defended on stage.
#
# This file is the control surface for the second kind. It creates one small
# table holding one item, which the demo app reads on each invocation to decide
# how badly to misbehave.
#
# COST: a DynamoDB on-demand table with a single item and no traffic bills
# nothing. Reads are one RRU per demo-app invocation, which at any plausible
# demo volume rounds to zero. There is no standing hourly charge here, and
# nothing in this file bills until the demo app is actually invoked.
#
# SAFETY, per the rules in CLAUDE.md:
#   * the demo app's function URL is AWS_IAM authenticated (D-011), so none of
#     this is reachable without credentials
#   * the faults are performance faults only -- latency, errors, memory. There
#     is no deliberately exploitable application logic anywhere
#   * no real secrets, no real data, nothing that outlives a teardown
#   * everything here carries Teardown = "true" through the default tags

locals {
  fault_table_name = "${local.demo_app_name}-faults"
}

resource "aws_dynamodb_table" "demo_app_faults" {
  name         = local.fault_table_name
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "config_key"

  attribute {
    name = "config_key"
    type = "S"
  }

  # Deliberately NOT deletion-protected, unlike the verdict store. That table is
  # an audit trail and losing it destroys evidence; this one holds a single
  # disposable config row that a script rewrites in a second. Protecting it would
  # only make the documented teardown harder.
  deletion_protection_enabled = false

  # No point-in-time recovery either, for the same reason. Recovering a fault
  # setting to a previous moment is not a thing anyone will ever want.

  tags = {
    Name    = local.fault_table_name
    Purpose = "phase 2.5 fault injection control"
  }
}

# --- Demo app read access -------------------------------------------------
#
# GetItem and nothing else. The application reads its own fault config; it
# cannot write one. That keeps the blast radius of the demo app itself at zero
# even though it is deliberately the least trustworthy component in the account,
# and it means the only way to turn faults ON is an operator with credentials
# running the documented script.
data "aws_iam_policy_document" "demo_app_faults" {
  statement {
    sid       = "ReadFaultConfig"
    actions   = ["dynamodb:GetItem"]
    resources = [aws_dynamodb_table.demo_app_faults.arn]
  }
}

resource "aws_iam_role_policy" "demo_app_faults" {
  name   = "faults"
  role   = aws_iam_role.demo_app.id
  policy = data.aws_iam_policy_document.demo_app_faults.json
}

output "fault_table_name" {
  description = "DynamoDB table holding the demo app's fault-injection config."
  value       = aws_dynamodb_table.demo_app_faults.name
}

output "fault_injection_next_step" {
  description = "What has to happen before fault injection actually works."
  value       = <<-EOT
    The demo app reads FAULT_TABLE from its environment, and Lambda snapshots
    environment variables into a published version. `terraform apply` sets the
    variable on $LATEST only -- the `live` alias serves published versions, so
    until the pipeline publishes a new one, injected faults will do nothing and
    say nothing.

    Run the pipeline once after this apply, then:

      python scripts/inject_fault.py status
      python scripts/inject_fault.py errors --rate 0.5
      python scripts/inject_fault.py off
  EOT
}
