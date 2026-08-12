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

  # Rollback on a failed or manually stopped deployment. Alarm-driven rollback
  # (DEPLOYMENT_STOP_ON_ALARM) needs an alarm_configuration block, and the
  # alarms it would watch are a Phase 2.5 deliverable -- they have to be tuned
  # against the demo app's fault injection to trip reliably. Listing the event
  # without the alarms would be decoration.
  auto_rollback_configuration {
    enabled = true
    events  = ["DEPLOYMENT_FAILURE", "DEPLOYMENT_STOP_ON_REQUEST"]
  }
}
