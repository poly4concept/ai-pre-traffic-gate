# Phase 1, increment 2 -- CodeDeploy canary for the demo app.
#
# This is the increment that proves traffic shifting works. Until a deployment
# has actually moved the `live` alias from one version to another while both
# were serving requests, "we can canary" is an assumption.
#
# Cost: CodeDeploy is free for Lambda deployments. AWS charges for CodeDeploy
# only on on-premises instances.

# --- Service role ---------------------------------------------------------

data "aws_iam_policy_document" "codedeploy_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["codedeploy.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "codedeploy" {
  name               = "${var.project_name}-codedeploy"
  assume_role_policy = data.aws_iam_policy_document.codedeploy_assume.json
}

# A deliberate exception to D-012 (prefer scoped inline policies over managed
# ones), and worth stating rather than hiding.
#
# D-012 applies to roles WE own, where we know the full set of actions our own
# code performs. This is a role AWS assumes on our behalf to operate its own
# service. The required permission set belongs to CodeDeploy and changes when
# CodeDeploy changes; hand-rolling it means a deployment that fails months from
# now with an AccessDenied on an action that did not exist when we wrote it.
#
# So we take the tighter of the two AWS-provided options. `...Limited` grants
# four actions (UpdateAlias, GetAlias, GetProvisionedConcurrencyConfig,
# DescribeAlarms) plus S3 reads for S3-sourced AppSpecs. The unrestricted
# `AWSCodeDeployRoleForLambda` additionally grants sns:Publish and broader
# Lambda access we have no use for.
#
# The general principle: least privilege means owning the permissions you can
# reason about, and delegating the ones the service owns. Writing a worse
# version of someone else's policy is not a security win.
resource "aws_iam_role_policy_attachment" "codedeploy" {
  role       = aws_iam_role.codedeploy.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSCodeDeployRoleForLambdaLimited"
}

# --- Deployment configuration ---------------------------------------------

# A custom config rather than a built-in one, for a specific and slightly
# undignified reason: the fastest built-in Lambda canary is
# CodeDeployDefault.LambdaCanary10Percent5Minutes, and five minutes of silence
# is a long time to fill on stage.
#
# This shifts `canary_percentage` of traffic, holds for
# `canary_interval_minutes`, then completes. At the defaults (10% / 1 minute)
# the whole deployment takes about a minute, which is long enough to watch the
# traffic split in a request loop and short enough to hold an audience.
#
# The demo value and the production value differ, and that is fine -- the
# deployment config is a separate resource from the deployment group precisely
# so the shape of a rollout can change without touching what is deployed.
# Phase 7 should raise this substantially for the company environment.
resource "aws_codedeploy_deployment_config" "canary" {
  deployment_config_name = "${var.project_name}-canary-${var.canary_percentage}pct-${var.canary_interval_minutes}min"
  compute_platform       = "Lambda"

  traffic_routing_config {
    type = "TimeBasedCanary"

    time_based_canary {
      percentage = var.canary_percentage
      interval   = var.canary_interval_minutes
    }
  }
}

# --- Application and deployment group -------------------------------------

resource "aws_codedeploy_app" "demo_app" {
  name             = "${var.project_name}-demo-app"
  compute_platform = "Lambda"
}

# Note what is NOT in here: the function name and the alias.
#
# For the Lambda compute platform the deployment TARGET comes from the AppSpec
# supplied at deploy time, not from the deployment group. The group holds only
# policy -- how to shift traffic, when to roll back. That separation is why the
# same group can later deploy the executor and the decision service without
# being redefined per function, and it is why `scripts/deploy_canary.py` has to
# construct an AppSpec rather than just naming a target.
resource "aws_codedeploy_deployment_group" "demo_app" {
  app_name               = aws_codedeploy_app.demo_app.name
  deployment_group_name  = "${var.project_name}-demo-app"
  service_role_arn       = aws_iam_role.codedeploy.arn
  deployment_config_name = aws_codedeploy_deployment_config.canary.deployment_config_name

  deployment_style {
    deployment_type   = "BLUE_GREEN"
    deployment_option = "WITH_TRAFFIC_CONTROL"
  }

  # Phase 2.5b filled in the gap this comment used to describe. The alarms now
  # exist and are tuned against the fault injection, so DEPLOYMENT_STOP_ON_ALARM
  # is a real event rather than decoration.
  #
  # The event is listed unconditionally while the alarm_configuration below is
  # what actually switches the behaviour on. Listing an event CodeDeploy can
  # never raise is harmless; wiring alarms nobody asked for is not.
  auto_rollback_configuration {
    enabled = true
    events = [
      "DEPLOYMENT_FAILURE",
      "DEPLOYMENT_STOP_ON_REQUEST",
      "DEPLOYMENT_STOP_ON_ALARM",
    ]
  }

  # The demo's centrepiece: break the canary, and CodeDeploy reverses the
  # traffic shift on its own while the audience watches.
  #
  # `enabled` is a variable defaulting to FALSE. With it on, any deploy started
  # while fault injection is active rolls itself straight back -- which is
  # correct, and thoroughly confusing if you had forgotten the injection was
  # still on. Switching it on is a deliberate demo step.
  #
  # `ignore_poll_alarm_failure = false` is the fail-closed choice, and it is the
  # same argument as everywhere else in this project: if CodeDeploy cannot READ
  # the alarms, it stops the deployment rather than assuming they are fine. An
  # unreadable alarm is not a passing alarm.
  alarm_configuration {
    enabled                   = var.canary_rollback_on_alarm
    alarms                    = local.demo_app_alarm_names
    ignore_poll_alarm_failure = false
  }
}
