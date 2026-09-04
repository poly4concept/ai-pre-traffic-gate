# Phase 5.3 -- where a human writes "I know, ship it anyway".
#
# COST: on-demand, no standing charge. A handful of items that expire within the
# hour, read at most once per pipeline execution. Rounds to zero.
#
# WHY A SEPARATE TABLE FROM THE VERDICTS
#
# The IAM boundary, and that is the whole reason. The gate must be able to WRITE
# verdicts and must NOT be able to write overrides -- a gate that could write its
# own override could approve itself, and the entire verdict path becomes
# decorative.
#
# Sharing one table would mean granting the gate PutItem on it and then trying
# to carve overrides back out with a `dynamodb:LeadingKeys` condition. That is
# possible and it is fiddly, and a fiddly IAM condition is a security control
# that nobody reviewing the repo can verify at a glance. Two tables makes the
# boundary a line anybody can read:
#
#     verdicts   gate: PutItem      executor: Query (via the GSI)
#     overrides  gate: GetItem      executor: -
#
# Nothing in the running system can write to this table. It is written by a
# person, from a laptop, with admin credentials.

resource "aws_dynamodb_table" "overrides" {
  name         = "${var.project_name}-overrides"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pipeline_execution_id"

  attribute {
    name = "pipeline_execution_id"
    type = "S"
  }

  # Scoped to one pipeline execution, which is the entire point of this table.
  # A CodePipeline execution ID is unique and never reused, so there is no way
  # to write a row here that affects the NEXT deploy -- the failure mode that
  # makes `gate_decision=allow` dangerous, where one urgent Friday bypass leaves
  # the gate off until somebody remembers.

  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }

  # TTL here is HOUSEKEEPING, not expiry. DynamoDB deletes expired items lazily
  # -- the documented window is up to 48 hours after the timestamp passes -- so
  # an override whose TTL has elapsed can remain a perfectly readable row for
  # two days. The gate checks `expires_at` itself in Python and that check is
  # the one that matters. A lazy deletion mechanism is not an access control.

  # No comma. AWS tag values are restricted to letters, digits, spaces and
  # `_ . : / = + - @`, and DynamoDB rejects anything else with a
  # ValidationException at CreateTable. Both `terraform validate` and
  # `terraform plan` pass -- the constraint belongs to the service, not to the
  # provider schema, so nothing local can see it (F-021).
  #
  # The explanation this used to carry is in the comment block above, which is
  # where it belonged anyway. A tag value is an index key, not documentation.
  tags = {
    Purpose = "Human overrides of gate verdicts"
  }
}

output "override_table_name" {
  description = "DynamoDB table holding per-execution human overrides."
  value       = aws_dynamodb_table.overrides.name
}
