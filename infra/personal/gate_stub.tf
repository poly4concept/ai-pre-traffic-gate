# Phase 1, increment 2 -- the hardcoded halt Lambda.
#
# The decision service's skeleton, with a variable where the verdict will go.
# Deployed as its own function with its own role from the start, because the
# central constraint of this project is that the thing which decides cannot be
# the thing which deploys. Merging them now and splitting them in Phase 5 would
# mean the split had never actually been tested.

locals {
  gate_stub_name = "${var.project_name}-gate"

  # `us.anthropic.claude-...` -> `anthropic.claude-...`
  #
  # An inference profile is not a thing you are granted access to on its own. A
  # call through `us.anthropic.*` authorises against BOTH the profile ARN and the
  # underlying foundation-model ARN, and the profile decides at request time
  # which region actually serves it. Naming only the profile produces an
  # AccessDeniedException that points at a foundation-model ARN you never wrote
  # down, which is a genuinely confusing hour (F-002).
  bedrock_foundation_model_id = replace(var.bedrock_model_id, "/^(us|eu|apac|global)[.]/", "")

  # If stripping a geography prefix changed the string, it was a profile ID.
  bedrock_is_profile = local.bedrock_foundation_model_id != var.bedrock_model_id
  bedrock_geography  = local.bedrock_is_profile ? split(".", var.bedrock_model_id)[0] : ""

  # Which foundation-model ARNs the call might authorise against.
  #
  # A cross-region profile can be served from any region in its geography, and
  # the request authorises against the foundation-model ARN in whichever one
  # wins -- so all of them must be named or the call fails intermittently, which
  # is a genuinely horrible way to discover an IAM gap. A bare model ID is served
  # only where it was called, so one region is enough.
  bedrock_geography_regions = {
    "us"   = ["us-east-1", "us-east-2", "us-west-2"]
    "eu"   = ["eu-north-1", "eu-west-1", "eu-central-1", "eu-west-3"]
    "apac" = ["ap-southeast-2", "ap-northeast-1", "ap-south-1", "ap-southeast-1"]
  }
  bedrock_model_regions = try(
    local.bedrock_geography_regions[local.bedrock_geography],
    [var.bedrock_region],
  )
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
  # GetMetricData covers all four Lambda metrics in a single request.
  # DescribeAlarms is needed for a reason worth stating: an empty alarm list is
  # ambiguous between "monitored and quiet" and "not monitored at all", and only
  # enumerating the alarms distinguishes them.
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

  # Phase 2.4b -- deploy cadence, read-only.
  #
  # Note what is absent and must stay absent: codedeploy:CreateDeployment. The
  # gate reads deployment history; the executor creates deployments. Splitting a
  # single service's actions between two roles this way is the concrete form of
  # "the thing that decides cannot be the thing that deploys" (D-016), and it is
  # only meaningful because the read half is genuinely useful on its own.
  #
  # Unlike the two collectors above, these actions DO support resource-level
  # permissions, so they are scoped to exactly one application and one
  # deployment group rather than `*`.
  statement {
    sid = "ReadDeployHistory"
    actions = [
      "codedeploy:ListDeployments",
      "codedeploy:BatchGetDeployments",
    ]
    resources = [
      "arn:aws:codedeploy:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:deploymentgroup:${aws_codedeploy_app.demo_app.name}/${aws_codedeploy_deployment_group.demo_app.deployment_group_name}",
      "arn:aws:codedeploy:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:application:${aws_codedeploy_app.demo_app.name}",
    ]
  }

  # Phase 3.4 -- write the verdict audit record. PutItem and nothing else.
  #
  # WHAT IS ABSENT IS THE DESIGN:
  #
  #   dynamodb:UpdateItem  -- cannot amend a verdict after the fact
  #   dynamodb:DeleteItem  -- cannot remove one
  #   dynamodb:GetItem     -- does not need to read its own history
  #   dynamodb:Query/Scan  -- likewise
  #
  # An audit record that the writer can later rewrite is not evidence, it is a
  # note. IAM is the outer of two independent guarantees here: the write itself
  # is also conditional on `attribute_not_exists(verdict_id)`, which stops
  # PutItem being used to overwrite -- something IAM cannot express, because
  # PutItem-that-creates and PutItem-that-replaces are the same action.
  #
  # Two mechanisms for one property is deliberate. IAM stops a different caller;
  # the condition stops this caller retrying into a contradiction.
  #
  # Reads belong to whoever inspects the trail -- a human, or the Phase 5
  # escalation path -- and are deliberately not granted to the thing that writes
  # it. The GSI ARN is included because DynamoDB treats index access as a
  # separate resource, and a policy naming only the table would fail the moment
  # anything queried by service name.
  statement {
    sid = "WriteVerdictAuditRecord"
    actions = [
      "dynamodb:PutItem",
    ]
    resources = [
      aws_dynamodb_table.verdicts.arn,
      "${aws_dynamodb_table.verdicts.arn}/index/*",
    ]
  }

  # Phase 5.2 -- telling a human. Publish only, to exactly one topic.
  #
  # Note what is absent: no sns:Subscribe, no sns:SetTopicAttributes, no
  # sns:CreateTopic. The gate can send a message to one address it does not
  # control and cannot change who receives it. A gate that could add a
  # subscriber could quietly redirect its own escalations, which is precisely
  # the capability an attacker who reached this function would want.
  statement {
    sid       = "PublishEscalation"
    actions   = ["sns:Publish"]
    resources = [aws_sns_topic.escalations.arn]
  }

  # Phase 3.5 -- the verdict call itself.
  #
  # One model, three regions, one action. Note what is absent: no
  # bedrock:CreateModelCustomizationJob, no bedrock:PutFoundationModelEntitlement,
  # no aws-marketplace:Subscribe. The gate can ask an existing model a question
  # and can do nothing else to the Bedrock control plane -- it cannot enable a
  # model, change an entitlement, or run up a bill on anything but inference.
  #
  # InvokeModelWithResponseStream is deliberately excluded. The verdict is a
  # single structured tool call that we validate as a whole; there is nothing to
  # stream, and granting it would widen the surface for no capability.
  statement {
    sid     = "InvokeVerdictModel"
    actions = ["bedrock:InvokeModel"]
    resources = concat(
      [
        for region in local.bedrock_model_regions :
        "arn:aws:bedrock:${region}::foundation-model/${local.bedrock_foundation_model_id}"
      ],
      # Only a profile ID needs a profile ARN. Granting one for a bare model ID
      # would name a resource that cannot exist -- harmless, but it would put a
      # line in the policy that no call ever authorises against, which is how
      # least-privilege reviews start being ignored.
      local.bedrock_is_profile ? [
        "arn:aws:bedrock:${var.bedrock_region}:${data.aws_caller_identity.current.account_id}:inference-profile/${var.bedrock_model_id}",
      ] : [],
    )
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

      # Deploy cadence is read from the CodeDeploy control plane, so it is the
      # one part of change context the change itself cannot influence.
      CODEDEPLOY_APP   = aws_codedeploy_app.demo_app.name
      CODEDEPLOY_GROUP = aws_codedeploy_deployment_group.demo_app.deployment_group_name

      # Phase 3.4. Absent means the gate still produces and logs a verdict but
      # records nothing -- which is a legitimate degraded mode, not a failure,
      # for the same reason a failed audit write does not halt a judged deploy.
      VERDICT_TABLE = aws_dynamodb_table.verdicts.name

      # Phase 5.2. Where a halt gets announced. Absent behaves like an absent
      # VERDICT_TABLE: the gate judges, records and halts exactly as before and
      # reports `not_configured` instead of sending -- a degraded mode, named
      # distinctly from `failed` so an unset topic cannot be misread in a log as
      # an email that did not arrive.
      ESCALATION_TOPIC_ARN = aws_sns_topic.escalations.arn

      # Phase 3.5. Which model forms the verdict. Env-driven so Phase 4 can
      # sweep several models over one fixture set without a code change.
      BEDROCK_MODEL_ID = var.bedrock_model_id

      # Separate from the Lambda's own region. Bedrock quota is provisioned per
      # region and is zero in most of them on this account, so the gate deploys
      # beside its pipeline and calls Bedrock wherever it can actually be served.
      BEDROCK_REGION = var.bedrock_region
    }
  }

  depends_on = [aws_iam_role_policy.gate_stub]
}
