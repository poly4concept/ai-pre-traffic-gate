# Phase 1, increment 2 -- the hardcoded halt Lambda.
#
# The decision service's skeleton, with a variable where the verdict will go.
# Deployed as its own function with its own role from the start, because the
# central constraint of this project is that the thing which decides cannot be
# the thing which deploys. Merging them now and splitting them in Phase 5 would
# mean the split had never actually been tested.

locals {
  gate_stub_name = "${var.project_name}-gate"
}

data "archive_file" "gate_stub" {
  type        = "zip"
  source_file = "${path.module}/../../services/gate_stub/handler.py"
  output_path = "${path.module}/build/gate_stub.zip"
}

# --- Execution role -------------------------------------------------------

resource "aws_iam_role" "gate_stub" {
  name               = local.gate_stub_name
  assume_role_policy = data.aws_iam_policy_document.demo_app_assume.json
}

resource "aws_cloudwatch_log_group" "gate_stub" {
  name              = "/aws/lambda/${local.gate_stub_name}"
  retention_in_days = 7
}

# This policy is the hard design constraint from CLAUDE.md, written down.
#
# Logs, and the CodePipeline job-result calls. That is the entire permission
# set. Specifically absent, and absent on purpose:
#
#   lambda:UpdateAlias        -- cannot move traffic
#   lambda:UpdateFunctionCode -- cannot deploy anything
#   codedeploy:*              -- cannot start or influence a deployment
#   iam:PassRole              -- cannot borrow a role that could do the above
#
# The reason this matters more than it looks: from Phase 2 on, this function's
# input includes commit messages, branch names, and file paths -- attacker-
# influenced text, on any repo that accepts pull requests. From Phase 3 that
# text is fed to a language model whose output steers a decision.
#
# Prompt injection against that model is not a hypothetical, and the design
# assumption here is that it will sometimes succeed. What makes that survivable
# is that the most a successful injection can achieve is a wrong verdict record.
# It cannot deploy, because the credentials to deploy are not in this process.
# The blast radius is bounded by IAM, not by the model behaving well.
#
# put_job_success_result is the one action that lets this function influence a
# pipeline, and it is bounded too: it can only report on a job the pipeline
# already handed it, and reporting success is exactly what happens anyway if the
# gate is absent. It cannot start a deploy that was not already running.
data "aws_iam_policy_document" "gate_stub" {
  statement {
    sid       = "Logs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.gate_stub.arn}:*"]
  }

  # These two carry no resource-level permissions in IAM -- the job ID in the
  # call is the authorisation, and CodePipeline only issues one to the function
  # its own action configuration names. `*` here is the only expressible form,
  # not an oversight.
  statement {
    sid = "ReportPipelineJobResult"
    actions = [
      "codepipeline:PutJobSuccessResult",
      "codepipeline:PutJobFailureResult",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "gate_stub" {
  name   = "gate"
  role   = aws_iam_role.gate_stub.id
  policy = data.aws_iam_policy_document.gate_stub.json
}

# --- Function -------------------------------------------------------------

resource "aws_lambda_function" "gate_stub" {
  function_name = local.gate_stub_name
  role          = aws_iam_role.gate_stub.arn
  handler       = "handler.lambda_handler"
  runtime       = "python3.13"
  architectures = ["arm64"]

  filename         = data.archive_file.gate_stub.output_path
  source_code_hash = data.archive_file.gate_stub.output_base64sha256

  # No alias and no published versions. The gate is not itself canaried -- a
  # partially-deployed gate would mean some deploys evaluated by the old logic
  # and some by the new, which is precisely the ambiguity an audit trail exists
  # to rule out. Gate changes are all-at-once and recorded.
  publish = false

  # 30s rather than the demo app's 5s. Nothing here needs it today, but Phase 3
  # adds a Bedrock call inside this handler and model latency at high effort is
  # measured in seconds. Sizing it now means the timeout does not become the
  # thing that "broke" when the model arrives.
  timeout     = 30
  memory_size = 256

  environment {
    variables = {
      GATE_DECISION = var.gate_decision
      GATE_MODE     = var.gate_mode
    }
  }

  depends_on = [aws_iam_role_policy.gate_stub]
}
