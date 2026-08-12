# Phase 1, increment 1 -- the demo application.
#
# A single Lambda, a `live` alias, and a function URL. No pipeline yet: the
# point of this increment is to have something real to deploy before there is
# anything doing the deploying.
#
# Cost: nothing with a standing hourly charge. Lambda and function URLs bill
# per invocation, the log group is the only thing that accrues without being
# called, and its retention is capped at 7 days for that reason.

locals {
  demo_app_name = "${var.project_name}-demo-app"
}

# Zipping the handler from Terraform is fine while the app is a single file
# with no dependencies. Once CodeBuild exists (increment 3) it produces the
# artifact instead, and this data source goes away. Noting that now so the
# swap does not look like an afterthought later.
data "archive_file" "demo_app" {
  type        = "zip"
  source_file = "${path.module}/../../services/demo_app/handler.py"
  output_path = "${path.module}/build/demo_app.zip"
}

# --- Execution role -------------------------------------------------------

data "aws_iam_policy_document" "demo_app_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "demo_app" {
  name               = local.demo_app_name
  assume_role_policy = data.aws_iam_policy_document.demo_app_assume.json
}

# Deliberately NOT the AWSLambdaBasicExecutionRole managed policy. That policy
# grants logs:CreateLogGroup on `*`, which lets the function write to any log
# group in the account. Since Terraform pre-creates the one log group this
# function needs, the role can be scoped to exactly that ARN and does not need
# CreateLogGroup at all.
#
# On its own this is a trivially small win -- the demo app is harmless. It
# matters because the same reasoning is what keeps the decision service unable
# to deploy anything in Phase 3, and it is easier to argue for that constraint
# from a codebase that already applies it everywhere.
data "aws_iam_policy_document" "demo_app_logs" {
  statement {
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.demo_app.arn}:*"]
  }
}

resource "aws_iam_role_policy" "demo_app_logs" {
  name   = "logs"
  role   = aws_iam_role.demo_app.id
  policy = data.aws_iam_policy_document.demo_app_logs.json
}

# --- Function -------------------------------------------------------------

# Created explicitly rather than letting Lambda create it on first invocation,
# so that retention is set from the start. A Lambda-created log group defaults
# to never expire, which is a slow cost leak and exactly the kind of thing the
# Phase 8 soak would otherwise discover the expensive way.
resource "aws_cloudwatch_log_group" "demo_app" {
  name              = "/aws/lambda/${local.demo_app_name}"
  retention_in_days = 7
}

resource "aws_lambda_function" "demo_app" {
  function_name = local.demo_app_name
  role          = aws_iam_role.demo_app.arn
  handler       = "handler.lambda_handler"
  runtime       = "python3.13"
  architectures = ["arm64"]

  filename         = data.archive_file.demo_app.output_path
  source_code_hash = data.archive_file.demo_app.output_base64sha256

  # Every apply that changes the code publishes an immutable numbered version.
  # Traffic shifting needs at least two versions to shift between, so this is
  # a hard requirement rather than a nicety.
  publish = true

  timeout     = 5
  memory_size = 128

  environment {
    variables = {
      APP_VERSION = var.demo_app_version
      COMMIT_SHA  = var.demo_app_commit_sha
    }
  }

  # The second ownership handoff, and the same reasoning as the alias below.
  #
  # From increment 3 the PIPELINE owns this function's code: CodeBuild produces
  # the package and the executor calls UpdateFunctionCode. Terraform's job is
  # to create the function and to own its *shape* -- runtime, memory, timeout,
  # role, environment. The archive_file above is now only a bootstrap payload,
  # so the function is never empty on first create.
  #
  # Without ignore_changes, `terraform apply` would push the local copy of
  # handler.py over whatever the pipeline last deployed. On a laptop that is
  # slightly out of date, that silently reverts production to an older commit
  # while reporting a clean apply.
  #
  # The general boundary, worth stating because it decides where every future
  # change goes:
  #
  #   Terraform ships INFRASTRUCTURE  -- what exists and how it is configured
  #   The pipeline ships APPLICATION  -- what code runs inside it
  #
  # This is also why CodeBuild deliberately holds no Lambda permissions. If the
  # build could deploy infrastructure, buildspec.yml -- a repository file that
  # any pull request can edit -- would become a second, unreviewed path to
  # changing the account.
  lifecycle {
    ignore_changes = [filename, source_code_hash]
  }

  depends_on = [aws_iam_role_policy.demo_app_logs]
}

# --- Alias ----------------------------------------------------------------

# The alias is the deployment target. Callers address `live`; CodeDeploy moves
# `live` between versions. Nothing outside this file should ever reference a
# bare version number.
resource "aws_lambda_alias" "live" {
  name             = "live"
  description      = "Traffic-shifting target. Owned by CodeDeploy once the pipeline exists."
  function_name    = aws_lambda_function.demo_app.function_name
  function_version = aws_lambda_function.demo_app.version

  # This lifecycle block is the single most important line in the file, and it
  # is the one people omit.
  #
  # From increment 2 onward CodeDeploy owns which version the alias points at,
  # and during a canary it also sets routing_config to split traffic. Both are
  # runtime state, not desired state. Without ignore_changes, the next
  # `terraform apply` -- including one triggered by a completely unrelated
  # change elsewhere in the stack -- would reset the alias to whatever version
  # Terraform last knew about. That is an instant, silent, unreviewed rollback
  # of production, and it presents as "the deploy worked and then undid
  # itself", which is a genuinely hard thing to diagnose after the fact.
  #
  # Terraform declares intent. CodeDeploy owns this particular piece of state.
  # Where two systems both believe they own a value, one of them has to yield
  # explicitly.
  lifecycle {
    ignore_changes = [function_version, routing_config]
  }
}

# --- Function URL ---------------------------------------------------------

# AWS_IAM auth, never NONE. Callers must sign requests with SigV4.
#
# CLAUDE.md's safety rule for the deliberately-vulnerable Phase 2.5 build is
# that it is never internet-reachable without auth. Setting that now, while the
# app is still harmless, means there is no later moment where someone has to
# remember to lock it down -- the vulnerable dependencies land in a service
# that is already closed.
#
# `qualifier` pins the URL to the alias. Without it the URL serves $LATEST,
# which CodeDeploy never touches, and the canary would shift traffic that no
# caller ever reaches -- a green deployment that moved nothing.
resource "aws_lambda_function_url" "demo_app_live" {
  function_name      = aws_lambda_function.demo_app.function_name
  qualifier          = aws_lambda_alias.live.name
  authorization_type = "AWS_IAM"
}
