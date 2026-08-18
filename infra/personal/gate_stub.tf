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

# Phase 2.2b: source_dir rather than source_file.
#
# The gate now imports the `signals` package, so the deployment package has to
# contain the whole decision service directory rather than a single file. This
# is also why the handler moved from services/gate_stub/ to
# services/decision_service/ -- the gate IS the decision service, and keeping
# the package importable from the zip root is what makes `from signals import
# ...` work identically in Lambda and in the test suite.
#
# __pycache__ is excluded because it is machine- and version-specific. Including
# it would change source_code_hash on every developer machine, producing
# spurious Terraform diffs and pointless redeployments.
data "archive_file" "gate_stub" {
  type        = "zip"
  source_dir  = "${path.module}/../../services/decision_service"
  output_path = "${path.module}/build/gate_stub.zip"
  excludes    = ["__pycache__", "**/__pycache__/**", "**/*.pyc"]
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

  # Phase 2.3 -- read-only Amazon Inspector access.
  #
  # Three calls, and all three are load-bearing rather than two of them being
  # convenience. ListFindings alone returns an empty list for a resource nobody
  # has ever scanned, which is indistinguishable from a clean bill of health.
  # BatchGetAccountStatus and ListCoverage are what establish that somebody was
  # actually looking before an empty list is allowed to mean anything.
  #
  # `*` because inspector2 does not support resource-level permissions on these
  # actions -- there is no narrower ARN to name. Every one of them is a read;
  # none can enable Inspector, suppress a finding, or alter its configuration,
  # so the gate still cannot change the state of anything.
  statement {
    sid = "ReadInspectorFindings"
    actions = [
      "inspector2:BatchGetAccountStatus",
      "inspector2:ListCoverage",
      "inspector2:ListFindings",
    ]
    resources = ["*"]
  }

  # Phase 2.4 -- read-only CloudWatch access for live target health.
  #
  # GetMetricData covers all four Lambda metrics in a single request. DescribeAlarms
  # is needed for a reason worth stating: an empty alarm list is ambiguous between
  # "monitored and quiet" and "not monitored at all", and only enumerating the
  # alarms distinguishes them.
  #
  # `*` because neither action supports resource-level permissions -- CloudWatch
  # metrics have no ARNs. Both are reads: the gate cannot create an alarm, change
  # a threshold, set an alarm state, or publish a metric. It cannot manufacture
  # the health evidence it is about to be judged on.
  statement {
    sid = "ReadTargetHealth"
    actions = [
      "cloudwatch:GetMetricData",
      "cloudwatch:DescribeAlarms",
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

      # Which service the gate is judging a deploy TO. Inspector findings and
      # (from 2.4) CloudWatch metrics are both scoped to this name. Passed in
      # rather than hardcoded in the handler so the gate can later front more
      # than one service without a code change.
      TARGET_SERVICE = local.demo_app_name

      # Amazon Inspector carries a standing per-function charge, so declining to
      # enable it is a legitimate choice rather than a misconfiguration. false
      # yields a SKIPPED signal ("we chose not to look"); true runs the real
      # collector, which reports UNAVAILABLE with a reason while Inspector is
      # switched off -- never a fabricated clean result.
      SECURITY_SCANNING = tostring(var.security_scanning)
    }
  }

  depends_on = [aws_iam_role_policy.gate_stub]
}
