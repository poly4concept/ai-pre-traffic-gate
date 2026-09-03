# Phase 3.4 -- the verdict audit table.
#
# COST, verified against the AWS Price List API (us-east-1, August 2026):
#
#   Write request units   $0.625 per million   (halved from $1.25 in late 2024)
#   Read request units    $0.125 per million
#   Storage               $0.25 per GB-month, FIRST 25 GB FREE
#   Point-in-time recovery $0.20 per GB-month of table size
#
# One record is roughly 5 KB, and writes bill per 1 KB, so 5 WRU per verdict.
#
#   development + demo      ~200 records/month   ->  ~$0.00
#   Phase 8 soak (48/day)  ~1,440 records/month  ->  ~$0.01
#   aggressive (500/day)  ~15,000 records/month  ->  ~$0.05
#
# STANDING COST IS ZERO. PAY_PER_REQUEST means an idle table bills nothing but
# storage, and 25 GB free covers this table for years. That is what makes it
# compatible with the no-standing-hourly-cost constraint in CLAUDE.md -- unlike a
# NAT gateway or always-on Fargate, there is no clock running.

resource "aws_dynamodb_table" "verdicts" {
  name = "${var.project_name}-verdicts"

  # On-demand, not provisioned. Provisioned capacity would bill per hour whether
  # or not a deploy happened, which is precisely the shape of cost this project
  # is not allowed to introduce. It is also the wrong fit: pipeline traffic is
  # bursty and near-zero between runs, which is the case on-demand exists for.
  billing_mode = "PAY_PER_REQUEST"

  # `terraform validate` warns that hash_key is deprecated in favour of a
  # `key_schema` block. That argument does NOT exist in the pinned provider
  # (hashicorp/aws 6.58.0) -- adding it fails with "Blocks of type key_schema are
  # not expected here", verified rather than assumed. So the warning names a
  # migration that is not yet available here. Revisit when the provider pin moves.
  hash_key = "verdict_id"

  attribute {
    name = "verdict_id"
    type = "S"
  }

  attribute {
    name = "service_name"
    type = "S"
  }

  attribute {
    name = "recorded_at"
    type = "S"
  }

  # Only attributes used as a key anywhere need declaring. DynamoDB is
  # schemaless for everything else, which is why the ~40 other fields in a
  # verdict record appear nowhere in this file.
  attribute {
    name = "pipeline_execution_id"
    type = "S"
  }

  # "Every verdict for this service, newest first" is the query the demo and the
  # Phase 8 analysis both need, and it is not answerable from the primary key --
  # verdict_id is a pipeline job ID, which has no ordering.
  #
  # A Scan would work perfectly well at demo volume. It is deliberately not used,
  # because the audience will copy whatever is on the slide into a table that
  # eventually has real traffic, and "it worked in the demo" is how people learn
  # to scan production tables.
  #
  # KEYS_ONLY, not ALL: the GSI answers "which verdicts and in what order", and
  # the caller then fetches the ones it wants by primary key. ALL would duplicate
  # every 5 KB record into the index and double both storage and write cost for
  # a projection nothing needs.
  global_secondary_index {
    name            = "by_service_recorded_at"
    hash_key        = "service_name"
    range_key       = "recorded_at"
    projection_type = "KEYS_ONLY"
  }

  # Phase 5.1. "Which verdict applies to the deploy I am about to perform?" --
  # the executor's only question, and the primary key cannot answer it.
  #
  # `verdict_id` is the GATE's CodePipeline job ID. Each pipeline ACTION gets
  # its own job ID, so the executor's is a different string for the same deploy
  # and it has no way to derive the gate's. The pipeline EXECUTION id is the one
  # identifier both actions genuinely share.
  #
  # Range key `recorded_at` so the query can take the newest: re-running the
  # Gate action inside one execution writes a second record, and the later one
  # is the current answer. Without it, "which of the two" would be arbitrary.
  #
  # Not made the table's primary key instead, which was the tempting
  # alternative -- it would give the executor a strongly consistent GetItem and
  # remove this index entirely. Rejected because it collapses retries: a second
  # gate attempt would be refused by the conditional write, and the audit trail
  # would silently keep the FIRST attempt's verdict. This table's purpose is
  # evidence, and losing a record to save an index is the wrong trade.
  #
  # Cost: KEYS_ONLY over ~200 records is a rounding error on a table already
  # costing about a cent a month, and on-demand means an unused index bills
  # nothing but its storage.
  global_secondary_index {
    name            = "by_pipeline_execution"
    hash_key        = "pipeline_execution_id"
    range_key       = "recorded_at"
    projection_type = "KEYS_ONLY"
  }

  # $0.0014/month at this table's size. Worth it: "how would you recover the
  # audit trail" is the first question a company security review asks, and the
  # answer "we could not" is a bad one for a table whose entire purpose is
  # evidence.
  point_in_time_recovery {
    enabled = true
  }

  # DELIBERATELY NO TTL BLOCK.
  #
  # An audit trail that deletes itself is not an audit trail. Storage is free at
  # this volume, so there is no cost argument for expiry either -- the only
  # reason to add one would be a retention policy, which is a Phase 7
  # company-environment decision and not something to guess at now.

  # NO server_side_encryption BLOCK, and that is not the same as unencrypted.
  #
  # DynamoDB always encrypts at rest. The `enabled` flag in that block means
  # "use a customer-managed KMS key", so writing `enabled = false` -- which is
  # the default -- would appear on a slide as though encryption were switched
  # off. Omitting the block says the same thing without the misreading.
  #
  # An AWS-owned key rather than a CMK because a CMK costs about $1/month
  # standing -- more than this entire table -- and buys key-policy control this
  # account has no use for. Phase 7 revisits it for the company environment,
  # where that control does matter.

  # Protects the audit trail from `terraform destroy`, which is the right default
  # for a table whose entire purpose is evidence. It does mean teardown is two
  # steps, so it is a variable rather than a literal:
  #
  #   terraform apply  -var verdict_store_deletion_protection=false
  #   terraform destroy
  deletion_protection_enabled = var.verdict_store_deletion_protection

  tags = {
    Name    = "${var.project_name}-verdicts"
    Purpose = "immutable verdict audit trail"
  }
}
