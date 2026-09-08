# Phase 2.5c -- Amazon Inspector, so the security signal is a real answer.
#
# WHY THIS STOPPED BEING OPTIONAL
#
# It was deferred as a nice-to-have. Phase 5.4's first real advisory run showed
# it is not. Every verdict came back carrying:
#
#   "Security findings cannot be assessed because Inspector is disabled on this
#    account, leaving vulnerability status unknown."
#
# The system prompt tells the model, correctly, that missing evidence should
# push an assessment UP. So a permanently unavailable signal put a floor under
# every verdict -- `safe-bump`, the one-line dependabot change that exists
# specifically to NOT be flagged, came back `medium`.
#
# That is the absent-signal principle producing an answer that is right and
# operationally useless. The fix is not to soften the prompt. It is to let the
# gate see.
#
# COST, MEASURED RATHER THAN ESTIMATED
#
# From the AWS Price List API, us-east-1, service code AmazonInspectorV2:
#
#   USE1-Lambda-Standard-Scanning   $0.000417 / hour / function  = $0.30 / month
#   USE1-Lambda-Code-Scanning       $0.00084  / hour / function  = $0.61 / month
#
# Three functions in this account (gate, executor, demo-app), standard scanning
# only: 3 x $0.30 = **$0.91/month**. This is a STANDING cost -- the first in the
# project -- and it accrues per hour whether or not anything is deployed.
#
# LAMBDA_CODE IS DELIBERATELY NOT ENABLED
#
# It is twice the price and answers a different question. Standard scanning
# reads the dependency manifest of the deployed package and reports known CVEs
# in third-party libraries, which is exactly the signal the gate consumes. Code
# scanning analyses the function's own source for injection flaws and hardcoded
# secrets -- interesting, and not what `SecurityFindings` models.
#
# It would also be the wrong thing to point at a repository that deliberately
# contains a prompt-injection fixture and a fault-injection switch.
#
# TEARDOWN
#
#   terraform -chdir=infra/personal apply -var="inspector_enabled=false"
#
# Disabling stops the hourly charge immediately. Existing findings are retained
# by Inspector for a period and then aged out; nothing here needs deleting.

resource "aws_inspector2_enabler" "lambda_standard" {
  count = var.inspector_enabled ? 1 : 0

  account_ids = [data.aws_caller_identity.current.account_id]

  # LAMBDA only. Not LAMBDA_CODE (see above), not EC2, not ECR -- there are no
  # instances or images in this account, and naming a resource type you do not
  # have is how a bill acquires a line nobody can explain later.
  resource_types = ["LAMBDA"]
}

output "inspector_status" {
  description = "Whether the gate's security signal can return a real answer."
  value       = var.inspector_enabled ? "Lambda standard scanning ENABLED -- ~$0.91/month for 3 functions. Findings take ~15 minutes to appear after first enablement." : "DISABLED -- the gate's security signal will report UNAVAILABLE, which pushes every verdict upward (see the header of inspector.tf)"
}
