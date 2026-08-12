# Phase 1, increment 3 -- CodePipeline, CodeBuild, and the executor.
#
# Wires the two levers built in increment 2 into an actual pipeline:
#
#   Source -> Build -> Gate -> Deploy
#   GitHub   CodeBuild  gate   executor -> CodeDeploy canary
#
# The Gate stage sits BEFORE Deploy, so a halt means the deploy stage never
# runs. That ordering is the whole point of the increment.
#
# Cost: CodePipeline V2 bills per action-execution-minute (100 free per month).
# CodeBuild general1.small on ARM bills per build-minute (100 free per month).
# The artifact bucket holds a few KB per run and expires objects at 30 days.
# Nothing here has a standing hourly charge.

locals {
  executor_name = "${var.project_name}-executor"
  pipeline_name = "${var.project_name}-pipeline"
}

# --- Source connection ----------------------------------------------------

# Terraform can create this, but CANNOT finish it. The resource comes up with
# status PENDING and stays there until a human completes an OAuth handshake in
# the console, authorising AWS to read the repository.
#
# That is not a Terraform limitation to work around -- it is an authorisation a
# machine is not permitted to grant itself. The same shape as the Bedrock use
# case form in FAILURES.md F-004: some gates deliberately require a person.
#
# Until it is authorised, the pipeline exists and every execution fails at the
# Source stage.
resource "aws_codeconnections_connection" "github" {
  name          = "${var.project_name}-github"
  provider_type = "GitHub"
}

# --- Artifact bucket ------------------------------------------------------

# How stages hand files to each other. Every stage output lands here and the
# next stage reads it back.
resource "aws_s3_bucket" "artifacts" {
  bucket        = "${var.project_name}-artifacts-${data.aws_caller_identity.current.account_id}"
  force_destroy = true # build artifacts are disposable; never block a teardown
}

# CodePipeline requires versioning on the artifact bucket. Not optional.
resource "aws_s3_bucket_versioning" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id
  versioning_configuration {
    status = "Enabled"
  }
}

# SSE-S3 rather than KMS, deliberately. A customer-managed key would add a
# monthly charge plus per-request costs, and would mean granting kms:Decrypt to
# every role that touches an artifact -- for build outputs that contain one
# public Python file. If this project ever carries a real application secret
# through the pipeline, revisit.
resource "aws_s3_bucket_server_side_encryption_configuration" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_public_access_block" "artifacts" {
  bucket                  = aws_s3_bucket.artifacts.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Versioning plus frequent pipeline runs is an unbounded cost leak otherwise.
resource "aws_s3_bucket_lifecycle_configuration" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id

  rule {
    id     = "expire-artifacts"
    status = "Enabled"
    filter {}

    expiration {
      days = 30
    }
    noncurrent_version_expiration {
      noncurrent_days = 7
    }
    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}

# --- CodeBuild ------------------------------------------------------------

data "aws_iam_policy_document" "codebuild_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["codebuild.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "codebuild" {
  name               = "${var.project_name}-codebuild"
  assume_role_policy = data.aws_iam_policy_document.codebuild_assume.json
}

resource "aws_cloudwatch_log_group" "codebuild" {
  name              = "/aws/codebuild/${var.project_name}-build"
  retention_in_days = 7
}

# Note what CodeBuild can NOT do: it has no Lambda permissions, no CodeDeploy
# permissions, and no ability to touch infrastructure. It reads source, writes
# an artifact, and writes logs.
#
# This matters more than it looks. The pipeline ships application code;
# Terraform ships infrastructure. If CodeBuild could call UpdateFunctionCode or
# assume a deploy role, `buildspec.yml` -- a file in the repository, editable by
# anyone who can open a pull request -- would become a second, unreviewed path
# to changing infrastructure. Keeping the build powerless is what keeps that
# boundary real rather than conventional.
data "aws_iam_policy_document" "codebuild" {
  statement {
    sid       = "Logs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.codebuild.arn}:*"]
  }

  statement {
    sid = "Artifacts"
    actions = [
      "s3:GetObject",
      "s3:GetObjectVersion",
      "s3:PutObject",
      "s3:GetBucketLocation",
    ]
    resources = [
      aws_s3_bucket.artifacts.arn,
      "${aws_s3_bucket.artifacts.arn}/*",
    ]
  }
}

resource "aws_iam_role_policy" "codebuild" {
  name   = "build"
  role   = aws_iam_role.codebuild.id
  policy = data.aws_iam_policy_document.codebuild.json
}

resource "aws_codebuild_project" "demo_app" {
  name          = "${var.project_name}-build"
  service_role  = aws_iam_role.codebuild.arn
  build_timeout = 10

  artifacts {
    type = "CODEPIPELINE"
  }

  environment {
    # ARM to match the arm64 Lambdas (D-013) and because it is cheaper per
    # build-minute at identical performance for this workload.
    type                        = "ARM_CONTAINER"
    compute_type                = "BUILD_GENERAL1_SMALL"
    image                       = "aws/codebuild/amazonlinux-aarch64-standard:3.0"
    image_pull_credentials_type = "CODEBUILD"
  }

  source {
    type      = "CODEPIPELINE"
    buildspec = "buildspec.yml"
  }

  logs_config {
    cloudwatch_logs {
      group_name = aws_cloudwatch_log_group.codebuild.name
    }
  }
}

# --- Executor -------------------------------------------------------------

resource "aws_iam_role" "executor" {
  name               = local.executor_name
  assume_role_policy = data.aws_iam_policy_document.demo_app_assume.json
}

resource "aws_cloudwatch_log_group" "executor" {
  name              = "/aws/lambda/${local.executor_name}"
  retention_in_days = 7
}

# This is the role that CAN deploy, and it is the only one in the project. Read
# it against the gate's policy in gate_stub.tf -- that contrast is the
# architecture.
#
# Every permission is scoped to a specific resource. The executor can update one
# function, move one alias, and create deployments in one deployment group. It
# cannot deploy anything else in the account, and it cannot grant itself the
# ability to.
#
# Absent on purpose: iam:PassRole. Without it the executor cannot hand a more
# powerful role to any service, which closes the usual privilege-escalation
# route out of a scoped role.
#
# Also absent: any S3 permission. The executor reads the build artifact using
# the short-lived credentials CodePipeline attaches to each job, so it holds no
# standing access to the artifact bucket at all.
data "aws_iam_policy_document" "executor" {
  statement {
    sid       = "Logs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.executor.arn}:*"]
  }

  statement {
    sid = "ReportPipelineJobResult"
    actions = [
      "codepipeline:PutJobSuccessResult",
      "codepipeline:PutJobFailureResult",
    ]
    resources = ["*"] # no resource-level permissions exist; the job ID is the authorisation
  }

  statement {
    sid = "PublishTargetFunction"
    actions = [
      "lambda:UpdateFunctionCode",
      "lambda:PublishVersion",
      "lambda:GetFunction",
      "lambda:GetFunctionConfiguration",
      "lambda:GetAlias",
    ]
    resources = [aws_lambda_function.demo_app.arn]
  }

  statement {
    sid     = "DriveCodeDeploy"
    actions = ["codedeploy:CreateDeployment", "codedeploy:GetDeployment"]
    resources = [
      "arn:aws:codedeploy:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:deploymentgroup:${aws_codedeploy_app.demo_app.name}/${aws_codedeploy_deployment_group.demo_app.deployment_group_name}",
    ]
  }

  # CreateDeployment validates the named config, which is a separate ARN type
  # from the deployment group. Omitting this produces an AccessDenied that names
  # the deploymentconfig ARN while the policy looks complete.
  statement {
    sid     = "ReadDeploymentConfig"
    actions = ["codedeploy:GetDeploymentConfig"]
    resources = [
      "arn:aws:codedeploy:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:deploymentconfig:${aws_codedeploy_deployment_config.canary.deployment_config_name}",
    ]
  }

  # The alias move is performed by CodeDeploy's own service role, not this one.
  # The executor asks for a deployment; CodeDeploy carries it out. Even the
  # thing holding deploy permissions does not itself hold lambda:UpdateAlias.
}

resource "aws_iam_role_policy" "executor" {
  name   = "executor"
  role   = aws_iam_role.executor.id
  policy = data.aws_iam_policy_document.executor.json
}

data "archive_file" "executor" {
  type        = "zip"
  source_file = "${path.module}/../../services/executor/handler.py"
  output_path = "${path.module}/build/executor.zip"
}

resource "aws_lambda_function" "executor" {
  function_name = local.executor_name
  role          = aws_iam_role.executor.arn
  handler       = "handler.lambda_handler"
  runtime       = "python3.13"
  architectures = ["arm64"]

  filename         = data.archive_file.executor.output_path
  source_code_hash = data.archive_file.executor.output_base64sha256

  # Like the gate, not itself canaried. A partially-deployed executor would mean
  # some deploys driven by old logic and some by new.
  publish = false

  # Downloading an artifact and starting a deployment, not waiting for one --
  # the continuation-token pattern means each invocation is short.
  timeout     = 60
  memory_size = 512

  environment {
    variables = {
      TARGET_FUNCTION  = aws_lambda_function.demo_app.function_name
      TARGET_ALIAS     = aws_lambda_alias.live.name
      CODEDEPLOY_APP   = aws_codedeploy_app.demo_app.name
      CODEDEPLOY_GROUP = aws_codedeploy_deployment_group.demo_app.deployment_group_name
    }
  }

  depends_on = [aws_iam_role_policy.executor]
}

# --- Pipeline -------------------------------------------------------------

data "aws_iam_policy_document" "codepipeline_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["codepipeline.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "codepipeline" {
  name               = "${var.project_name}-codepipeline"
  assume_role_policy = data.aws_iam_policy_document.codepipeline_assume.json
}

# The pipeline orchestrates; it does not deploy. It can start a build, invoke
# two specific Lambdas, and move artifacts. Nothing here can change the demo app
# or move an alias -- those permissions live only in the executor's role, and
# the pipeline reaches them only by invoking the executor.
data "aws_iam_policy_document" "codepipeline" {
  statement {
    sid = "Artifacts"
    actions = [
      "s3:GetObject",
      "s3:GetObjectVersion",
      "s3:PutObject",
      "s3:GetBucketVersioning",
      "s3:GetBucketLocation",
    ]
    resources = [
      aws_s3_bucket.artifacts.arn,
      "${aws_s3_bucket.artifacts.arn}/*",
    ]
  }

  statement {
    sid       = "UseSourceConnection"
    actions   = ["codeconnections:UseConnection"]
    resources = [aws_codeconnections_connection.github.arn]
  }

  statement {
    sid       = "RunBuild"
    actions   = ["codebuild:StartBuild", "codebuild:BatchGetBuilds"]
    resources = [aws_codebuild_project.demo_app.arn]
  }

  statement {
    sid     = "InvokeGateAndExecutor"
    actions = ["lambda:InvokeFunction"]
    resources = [
      aws_lambda_function.gate_stub.arn,
      aws_lambda_function.executor.arn,
    ]
  }
}

resource "aws_iam_role_policy" "codepipeline" {
  name   = "pipeline"
  role   = aws_iam_role.codepipeline.id
  policy = data.aws_iam_policy_document.codepipeline.json
}

resource "aws_codepipeline" "main" {
  name     = local.pipeline_name
  role_arn = aws_iam_role.codepipeline.arn

  # V2 over V1: V1 charges $1/month per active pipeline whether or not it runs.
  # V2 bills per action-execution-minute with a monthly free allowance, which
  # for a demo-frequency pipeline is effectively zero.
  pipeline_type = "V2"

  artifact_store {
    type     = "S3"
    location = aws_s3_bucket.artifacts.bucket
  }

  stage {
    name = "Source"

    action {
      name             = "GitHub"
      category         = "Source"
      owner            = "AWS"
      provider         = "CodeStarSourceConnection"
      version          = "1"
      output_artifacts = ["source"]

      configuration = {
        ConnectionArn    = aws_codeconnections_connection.github.arn
        FullRepositoryId = var.github_repository
        BranchName       = var.github_branch
        # Webhook-driven rather than polled. Polling costs an API call every
        # minute forever and adds up to a minute of latency.
        DetectChanges = true
      }
    }
  }

  stage {
    name = "Build"

    action {
      name             = "Build"
      category         = "Build"
      owner            = "AWS"
      provider         = "CodeBuild"
      version          = "1"
      input_artifacts  = ["source"]
      output_artifacts = ["package"]

      configuration = {
        ProjectName = aws_codebuild_project.demo_app.name
      }
    }
  }

  # The reason this project exists. Everything before it is ordinary CI;
  # everything after it only runs with this stage's consent.
  #
  # In Phase 3 the function invoked here starts collecting signals and asking a
  # model. The pipeline definition does not change -- that is the test of
  # whether this seam is in the right place.
  stage {
    name = "Gate"

    action {
      name     = "Gate"
      category = "Invoke"
      owner    = "AWS"
      provider = "Lambda"
      version  = "1"

      configuration = {
        FunctionName = aws_lambda_function.gate_stub.function_name
      }
    }
  }

  stage {
    name = "Deploy"

    action {
      name            = "Deploy"
      category        = "Invoke"
      owner           = "AWS"
      provider        = "Lambda"
      version         = "1"
      input_artifacts = ["package"]

      configuration = {
        FunctionName = aws_lambda_function.executor.function_name
      }
    }
  }
}
