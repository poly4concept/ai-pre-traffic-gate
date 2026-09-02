# Phase 2.5b -- CloudWatch alarms tuned to trip on injected faults.
#
# WHAT THESE ARE FOR
#
# 2.5a gave the demo app a switch that makes it genuinely misbehave. These are
# what NOTICE. Without them the gate reads metrics and nothing else, and the
# `has_active_alarm` signal it was built to consume is permanently false --
# which is not "the service is fine", it is "nobody is watching", and those must
# not look the same (D-027).
#
# They also make the canary rollback demo real: break version N+1, alarms fire,
# CodeDeploy reverses the traffic shift on stage. See var.canary_rollback_on_alarm.
#
# TUNING, AND WHY THESE NUMBERS
#
# The thresholds are set against what 2.5a actually produces, not against
# general good practice:
#
#   error rate   5%     -- injection at 50% clears it by 10x. Even a 10% canary
#                          running at 100% errors lands near 10% overall, so a
#                          fully-broken canary trips it while ordinary noise on
#                          a healthy function (which is 0%) does not.
#   p99 latency  2000ms -- the demo app answers in tens of milliseconds. The
#                          `slow` dial adds 3000ms, so the gap either side of
#                          this threshold is enormous and the alarm is never
#                          ambiguous.
#   throttles    >= 1   -- any throttle at this traffic level is abnormal.
#
# One 60-second period, not the usual three. Three periods is right for
# production, where flapping wakes people up; it is wrong for a stage demo where
# three minutes of silence loses the room. Both numbers are variables so the
# company rollout can be less twitchy without a code change.
#
# COST: three alarms at standard resolution, $0.10 per alarm-month. The
# error-rate alarm uses metric math and is billed per metric it references, so
# budget roughly $0.40/month rather than $0.30. Trivial, but it is the first
# recurring charge in this stack that exists whether or not anything is
# deployed, so it is stated rather than absorbed.

# --- Error rate ------------------------------------------------------------
#
# A RATE, not a count. A count threshold silently means different things at
# different traffic levels: "10 errors in a minute" is catastrophic at 20
# requests and unremarkable at 200,000. The gate already reports error_rate_pct
# for the same reason (D-026), and an alarm that disagreed with the metric the
# gate reads would be its own kind of confusing.
resource "aws_cloudwatch_metric_alarm" "demo_app_error_rate" {
  alarm_name          = "${local.demo_app_name}-error-rate"
  alarm_description   = "Demo app error rate above ${var.alarm_error_rate_pct}% -- Phase 2.5b"
  comparison_operator = "GreaterThanThreshold"
  threshold           = var.alarm_error_rate_pct
  evaluation_periods  = var.alarm_evaluation_periods

  # THE MOST IMPORTANT LINE IN THIS FILE.
  #
  # `notBreaching` is the tempting default and it is the absent-vs-zero trap in
  # CloudWatch's own vocabulary: with no traffic there is no error rate to
  # compute, and treating that as OK would report a service nobody has invoked
  # as healthy. `missing` leaves the alarm in INSUFFICIENT_DATA, which is the
  # honest state -- and which the Phase 2.4 collector already distinguishes from
  # both OK and ALARM.
  treat_missing_data = "missing"

  metric_query {
    id          = "rate"
    expression  = "100 * (errors / invocations)"
    label       = "Error rate (%)"
    return_data = true
  }

  metric_query {
    id = "errors"
    metric {
      namespace   = "AWS/Lambda"
      metric_name = "Errors"
      # The gate matches alarms on this dimension rather than on the alarm name
      # (D-027), so it is what makes the alarm visible to the verdict layer at
      # all. An alarm without it would fire correctly and be invisible.
      dimensions = { FunctionName = local.demo_app_name }
      period     = var.alarm_period_seconds
      stat       = "Sum"
    }
  }

  metric_query {
    id = "invocations"
    metric {
      namespace   = "AWS/Lambda"
      metric_name = "Invocations"
      dimensions  = { FunctionName = local.demo_app_name }
      period      = var.alarm_period_seconds
      stat        = "Sum"
    }
  }

  tags = { Name = "${local.demo_app_name}-error-rate" }
}

# --- Latency ---------------------------------------------------------------
#
# p99 rather than Average. An average hides the tail: at a 50% fault rate with
# 3000ms of injected delay, the mean roughly doubles while the p99 goes through
# the roof. Users experience the tail, and so should the alarm.
resource "aws_cloudwatch_metric_alarm" "demo_app_latency" {
  alarm_name          = "${local.demo_app_name}-p99-latency"
  alarm_description   = "Demo app p99 duration above ${var.alarm_p99_latency_ms}ms -- Phase 2.5b"
  comparison_operator = "GreaterThanThreshold"
  threshold           = var.alarm_p99_latency_ms
  evaluation_periods  = var.alarm_evaluation_periods
  treat_missing_data  = "missing"

  namespace          = "AWS/Lambda"
  metric_name        = "Duration"
  dimensions         = { FunctionName = local.demo_app_name }
  period             = var.alarm_period_seconds
  extended_statistic = "p99"

  tags = { Name = "${local.demo_app_name}-p99-latency" }
}

# --- Throttles -------------------------------------------------------------
#
# A count is right here, unlike the error rate: one throttle is one request that
# never ran, and at this traffic level there is no benign reason for it.
#
# Kept separate from the error alarm even though the gate counts throttles as
# errors when computing its rate (D-028). The gate is summarising for a model;
# an operator looking at alarms needs to know WHICH failure happened, because a
# throttle and an exception have completely different fixes.
resource "aws_cloudwatch_metric_alarm" "demo_app_throttles" {
  alarm_name          = "${local.demo_app_name}-throttles"
  alarm_description   = "Demo app is being throttled -- Phase 2.5b"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  threshold           = 1
  evaluation_periods  = var.alarm_evaluation_periods
  treat_missing_data  = "missing"

  namespace   = "AWS/Lambda"
  metric_name = "Throttles"
  dimensions  = { FunctionName = local.demo_app_name }
  period      = var.alarm_period_seconds
  statistic   = "Sum"

  tags = { Name = "${local.demo_app_name}-throttles" }
}

# Exposed so the CodeDeploy deployment group can watch exactly these, and so a
# rename here cannot silently detach the rollback wiring.
locals {
  demo_app_alarm_names = [
    aws_cloudwatch_metric_alarm.demo_app_error_rate.alarm_name,
    aws_cloudwatch_metric_alarm.demo_app_latency.alarm_name,
    aws_cloudwatch_metric_alarm.demo_app_throttles.alarm_name,
  ]
}
