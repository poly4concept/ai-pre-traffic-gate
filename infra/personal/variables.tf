variable "aws_region" {
  description = "Region for all project resources."
  type        = string
  default     = "us-east-1"
}

variable "aws_account_id" {
  description = "Personal AWS account the project is deployed into. Acts as a misfire guard on the provider."
  type        = string
  default     = "594380318102"
}

variable "project_name" {
  description = "Prefix for all resource names."
  type        = string
  default     = "ai-pre-traffic-gate"
}

# The gate's operating mode. Shadow mode is a permanent first-class mode, not a
# temporary flag: in shadow the gate records the verdict it would have issued
# and takes no action at all.
#
#   shadow    - record only, no action, no notification
#   advisory  - record and notify, but never block the pipeline
#   enforcing - record, notify, and act on the verdict
#
# Nothing reads this until Phase 3. It is declared now so that every stack from
# here on is written mode-aware rather than retrofitted later.
# Bumping this is how we produce a visibly different deployment without
# changing any behaviour. It is the input to the "deploy something, watch the
# canary shift, roll it back" loop that Phase 1 has to prove works.
#
# EDIT THIS DEFAULT. Do not pass `-var demo_app_version=...` on the command
# line.
#
# A `-var` flag applies once and is persisted nowhere. The value lands in AWS,
# the repository still says something else, and the next plain `terraform apply`
# -- possibly days later, possibly for an unrelated change -- quietly reverts
# it. Because `publish = true`, that revert also mints a new Lambda version
# containing the older label, so the version numbers stop corresponding to
# anything meaningful.
#
# The rule this is a special case of: if a value has to survive the next apply,
# it belongs in the repository. Terraform state records what was applied, not
# what you meant. See FAILURES.md F-006.
variable "demo_app_version" {
  description = "Version label baked into the demo app's responses. Edit here, never via -var."
  type        = string
  default     = "1.1.0"
}

# Populated by CodeBuild from CODEBUILD_RESOLVED_SOURCE_VERSION once the
# pipeline exists. Until then it is a placeholder, which is honest: right now
# nothing ties a deployed artifact back to a commit, and Phase 2's change
# context collector will need exactly that link.
variable "demo_app_commit_sha" {
  description = "Commit the demo app was built from. Placeholder until CodeBuild sets it."
  type        = string
  default     = "local-apply"
}

# --- Pipeline source ------------------------------------------------------

variable "github_repository" {
  description = "Source repository as owner/repo. Must match GitHub exactly, including case."
  type        = string
  default     = "poly4concept/ai-pre-traffic-gate"

  validation {
    condition     = can(regex("^[^/]+/[^/]+$", var.github_repository))
    error_message = "github_repository must be in owner/repo form, with no leading https:// or trailing .git."
  }
}

variable "github_branch" {
  description = "Branch the pipeline deploys from."
  type        = string
  default     = "main"
}

# --- Canary shape ---------------------------------------------------------

# Split out as variables so the demo cadence and a realistic production cadence
# are the same code with different numbers. The fastest built-in Lambda canary
# config is 10% for 5 minutes; these defaults exist to make the shift watchable
# on stage rather than because 1 minute is a sensible bake time.
variable "canary_percentage" {
  description = "Percent of traffic sent to the new version during the canary phase."
  type        = number
  default     = 10

  validation {
    condition     = var.canary_percentage > 0 && var.canary_percentage < 100
    error_message = "canary_percentage must be between 1 and 99. 100 is not a canary."
  }
}

variable "canary_interval_minutes" {
  description = "Minutes to hold the canary split before shifting the remainder."
  type        = number
  default     = 1

  validation {
    condition     = var.canary_interval_minutes >= 1
    error_message = "canary_interval_minutes must be at least 1 (CodeDeploy's minimum granularity)."
  }
}

# --- Gate stub ------------------------------------------------------------

# The stand-in for a verdict until Phase 3. Note there is no "unset" option and
# no default of "allow": the Lambda fails closed on anything it does not
# recognise, so the only way to permit a deploy is to say so explicitly.
variable "gate_decision" {
  description = "Hardcoded verdict for the Phase 1 gate stub: allow | halt."
  type        = string
  default     = "allow"

  validation {
    condition     = contains(["allow", "halt"], var.gate_decision)
    error_message = "gate_decision must be exactly 'allow' or 'halt'."
  }
}

# Amazon Inspector bills per scanned function per hour, so this is the one
# signal collector whose existence costs money whether or not a deploy happens.
# Verified from the AWS Price List API (us-east-1, August 2026):
#
#   Lambda standard scanning (dependencies)   $0.00042/hour  ~= $0.31/month
#   Lambda code scanning (application logic)  $0.00084/hour  ~= $0.61/month
#
# Standard scanning only is the intended configuration. Phase 2.5 synthesises
# vulnerable *dependencies*, which is exactly what standard scanning finds, and
# CLAUDE.md forbids deliberately exploitable application logic -- so code
# scanning would cost double to find nothing by design.
#
# Setting this false makes the security signal SKIPPED rather than UNAVAILABLE:
# "we chose not to look" is a different statement from "we looked and could not
# tell", and neither may be read as "nothing wrong".
variable "security_scanning" {
  description = "Whether the gate consults Amazon Inspector. Requires Inspector to be enabled."
  type        = bool
  default     = true
}

variable "gate_mode" {
  description = "Operating mode of the deployment gate: shadow | advisory | enforcing."
  type        = string
  default     = "shadow"

  validation {
    condition     = contains(["shadow", "advisory", "enforcing"], var.gate_mode)
    error_message = "gate_mode must be one of: shadow, advisory, enforcing."
  }
}

# --- Verdict audit store --------------------------------------------------

# True by default: the audit table holds the evidence this whole project
# produces, and `terraform destroy` should not be able to take it out by
# accident. Teardown is therefore deliberately two steps -- set this false,
# apply, then destroy. Documented in docs/runbooks/.
variable "verdict_store_deletion_protection" {
  description = "Whether the verdict audit table resists terraform destroy."
  type        = bool
  default     = true
}
