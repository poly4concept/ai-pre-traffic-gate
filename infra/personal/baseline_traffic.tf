# Phase 5.4b -- a heartbeat, so the health signal is a real answer.
#
# WHY
#
# The other half of the same problem as inspector.tf. Every advisory verdict
# said:
#
#   "The target service has no measurable traffic in the last 60 minutes, making
#    health metrics unknown rather than healthy."
#
# Correct, and permanently true of a demo app nobody invokes. Two of the gate's
# four signals were unavailable on every run, and the prompt's rule 1 -- missing
# evidence pushes risk UP -- turned that into a `medium` floor on ordinary
# changes.
#
# `drive_traffic.py` fixes it for the duration of one command. That is fine for
# testing and wrong for a stage demo, where the thing you least want is a
# prerequisite you have to remember to run before the interesting part.
#
# WHAT THIS IS NOT
#
# Not the Phase 8 load generator. That one varies traffic by time of day,
# injects scheduled fault windows, and drives the pipeline across the full
# scenario mix, and it needs a costed plan and sign-off first. This is one
# invocation a minute so the error-rate denominator is not zero.
#
# COST: nothing measurable, and the arithmetic is worth showing rather than
# asserting.
#
#   invocations   60/hr x 730hr            = 43,800 / month   (free tier: 1M)
#   compute       43,800 x ~2ms x 128MB    = ~11 GB-seconds   (free tier: 400k)
#   scheduler     43,800 invocations       = free             (free tier: 14M)
#   logs          ~9 MB/month ingestion    = free             (free tier: 5GB)
#   DynamoDB      43,800 fault-config reads x $0.25/M ~= $0.01/month
#
# It is still a STANDING resource that runs when nobody is watching, which is
# why it has its own switch and its own teardown line.
#
# TEARDOWN
#
#   terraform -chdir=infra/personal apply -var="baseline_traffic_enabled=false"

locals {
  baseline_traffic_name = "${var.project_name}-baseline-traffic"
}

# --- The role the scheduler assumes to invoke the function ----------------
#
# Narrow in the same way every other role here is: one action, one resource,
# and that resource is the ALIAS rather than the function. Invoking `$LATEST`
# would exercise code no traffic reaches and produce metrics for a version the
# gate does not judge -- the same photocopy trap that made the fault switch look
# broken in F-016.

data "aws_iam_policy_document" "baseline_traffic_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["scheduler.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
  }
}

resource "aws_iam_role" "baseline_traffic" {
  count = var.baseline_traffic_enabled ? 1 : 0

  name               = local.baseline_traffic_name
  assume_role_policy = data.aws_iam_policy_document.baseline_traffic_assume.json
}

resource "aws_iam_role_policy" "baseline_traffic" {
  count = var.baseline_traffic_enabled ? 1 : 0

  name = "invoke-demo-app-alias"
  role = aws_iam_role.baseline_traffic[0].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid      = "InvokeDemoAppAlias"
      Effect   = "Allow"
      Action   = "lambda:InvokeFunction"
      Resource = aws_lambda_alias.live.arn
    }]
  })
}

# --- Stop Lambda retrying failed async invocations ------------------------
#
# THE LINE THAT KEEPS THE ERROR RATE HONEST.
#
# EventBridge Scheduler invokes Lambda asynchronously, and Lambda's default for
# an async invocation that raises is to retry it twice. So with fault injection
# at 50%, one faulted request would publish THREE Errors, and the observed error
# rate would read roughly three times its real value.
#
# The gate reads that error rate and forms a verdict from it. An inflated
# denominator-free metric is exactly the class of lie this project keeps finding
# (F-016, F-018), arriving this time through a retry default nobody chose.
#
# Zero retries also matches what `drive_traffic.py` does deliberately: a faulted
# invocation is a real result, not something to retry.
resource "aws_lambda_function_event_invoke_config" "demo_app_no_retries" {
  count = var.baseline_traffic_enabled ? 1 : 0

  function_name          = aws_lambda_alias.live.function_name
  qualifier              = aws_lambda_alias.live.name
  maximum_retry_attempts = 0
}

# --- The heartbeat --------------------------------------------------------

resource "aws_scheduler_schedule" "baseline_traffic" {
  count = var.baseline_traffic_enabled ? 1 : 0

  name        = local.baseline_traffic_name
  description = "One invocation a minute so the demo app's health signal has a denominator"

  # OFF, not FLEXIBLE. A flexible window would let AWS spread invocations over
  # several minutes, which is kinder to AWS and useless here: the point is an
  # even trickle so any 60-minute window the gate looks at contains a similar
  # number of data points.
  flexible_time_window {
    mode = "OFF"
  }

  # `rate(1 minute)` and `rate(5 minutes)`. AWS requires the singular for one
  # and the plural for anything else, and rejects the mismatch at apply time
  # with a ValidationException -- another service-side rule no local check can
  # see (F-021, same shape). The default of 1 hid it; any other value would not
  # have.
  schedule_expression = format(
    "rate(%d minute%s)",
    var.baseline_traffic_minutes,
    var.baseline_traffic_minutes == 1 ? "" : "s",
  )
  schedule_expression_timezone = "UTC"

  target {
    arn      = aws_lambda_alias.live.arn
    role_arn = aws_iam_role.baseline_traffic[0].arn

    # Marked, so a human reading a log line or a fault-injection tally can tell
    # heartbeat traffic from a real request or from `drive_traffic.py`.
    input = jsonencode({
      source = "baseline-traffic"
      note   = "scheduled heartbeat so target health has a denominator"
    })

    # Scheduler's own retries of the InvokeFunction CALL, which is a different
    # thing from Lambda retrying the FUNCTION (handled above). Zero for the same
    # reason: a retried invocation is an extra data point that did not really
    # happen, and this exists to measure.
    retry_policy {
      maximum_retry_attempts = 0
    }
  }
}

output "baseline_traffic_status" {
  description = "Whether the demo app has a heartbeat, and what it costs."
  value       = var.baseline_traffic_enabled ? "ENABLED -- one invocation every ${var.baseline_traffic_minutes} minute(s), ~${floor(43800 / var.baseline_traffic_minutes)} per month. Inside the free tier; ~$0.01/month of DynamoDB reads." : "DISABLED -- the demo app is idle, so target health reports UNKNOWN and every verdict is pushed upward"
}
