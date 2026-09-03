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
# Phase 3.5 changed what this means. It used to BE the verdict; it is now a
# manual override, and the default is now empty rather than "allow".
#
# The direction of the default flipped with it. Before 3.5 an absent value meant
# HALT, because a gate with no way to form an opinion has not approved anything.
# The gate now forms its own opinion, so absent means "no human intervened, use
# the model's verdict" -- and that verdict fails closed on its own when Bedrock
# is unreachable or the signals are missing.
#
# Fail-closed did not weaken; it moved down to where the judgement happens. What
# survives here is the kill switch: "halt" still stops the pipeline in enforcing
# mode no matter what the model thinks, which is both the Phase 1 demo and the
# thing you want on the day the model is wrong.
variable "gate_decision" {
  description = "Manual override for the gate: \"\" (none, use the model) | allow | halt."
  type        = string
  default     = ""

  validation {
    condition     = contains(["", "allow", "halt"], var.gate_decision)
    error_message = "gate_decision must be \"\" (no override), 'allow', or 'halt'."
  }
}

# Which model produces the verdict.
#
# A variable rather than a constant because Phase 4 picks the real one on
# measured over-flagging rate and cost per verdict, not on reputation -- and the
# eval harness has to sweep several models over one fixture set without a code
# change.
#
# Must be an inference-profile ID (the `us.` prefix), not a bare model ID. Most
# current models are INFERENCE_PROFILE-only and a bare `anthropic.*` ID fails
# with a ValidationException that does not mention profiles at all (F-002).
variable "bedrock_model_id" {
  description = "Bedrock model or inference-profile ID used for the risk verdict."
  type        = string
  default     = "us.anthropic.claude-haiku-4-5-20251001-v1:0"

  # Both forms are permitted, and the difference is not cosmetic:
  #
  #   us.anthropic.claude-...   an INFERENCE PROFILE. Routes across a geography,
  #                             and authorises against both the profile ARN and
  #                             the underlying foundation-model ARN wherever it
  #                             lands (F-002).
  #   amazon.nova-lite-v1:0     a BARE MODEL ID. Served only in the region
  #                             called, authorises against one ARN.
  #
  # The original validation here demanded a profile prefix. That was correct
  # advice and became a bug the moment the only combination this account can
  # actually serve turned out to be a bare Nova ID in eu-north-1 -- the rule
  # would have rejected the one thing that works.
  validation {
    condition     = can(regex("^([a-z]+[.])?[a-z0-9-]+[.][a-z0-9.:_-]+$", var.bedrock_model_id))
    error_message = "bedrock_model_id must be a model ID (amazon.nova-lite-v1:0) or profile ID (us.anthropic.claude-haiku-4-5-20251001-v1:0)."
  }
}

# Which region the gate calls Bedrock in -- NOT necessarily where it runs.
#
# Bedrock token quotas are provisioned per region, and on this account they are
# zero nearly everywhere (F-014). The Lambda therefore has to be able to reach a
# region that works while staying deployed next to the pipeline it gates.
#
# Cross-region latency is a real cost of this and worth stating: eu-north-1 from
# us-east-1 adds roughly 100-150ms round trip. Against a model call measured in
# seconds it is noise, and the 30s Lambda timeout absorbs it. It would not be
# noise for anything on a request path.
variable "bedrock_region" {
  description = "AWS region to call Bedrock in. May differ from the deployment region."
  type        = string
  default     = "us-east-1"
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

# --- Phase 2.5b: alarm tuning ---------------------------------------------
#
# Variables rather than constants because the right values differ by audience.
# A stage demo wants an alarm that trips inside one minute; a company rollout
# wants one that does not page anyone over a blip. Same alarms, different
# numbers, no code change.

variable "alarm_error_rate_pct" {
  description = "Error-rate percentage above which the demo app alarm fires."
  type        = number
  default     = 5

  validation {
    condition     = var.alarm_error_rate_pct > 0 && var.alarm_error_rate_pct < 100
    error_message = "alarm_error_rate_pct must be between 0 and 100 exclusive."
  }
}

variable "alarm_p99_latency_ms" {
  description = "p99 duration in milliseconds above which the demo app alarm fires."
  type        = number
  default     = 2000
}

variable "alarm_period_seconds" {
  description = "Metric period for the demo app alarms."
  type        = number
  default     = 60

  # 60 is the finest STANDARD-resolution period. Anything below it silently
  # becomes a high-resolution alarm at roughly three times the price, which is a
  # surprising way to find out you changed the billing model.
  validation {
    condition     = var.alarm_period_seconds >= 60
    error_message = "alarm_period_seconds below 60 makes these high-resolution alarms, which cost more."
  }
}

variable "alarm_evaluation_periods" {
  description = "Consecutive periods breaching before the alarm fires."
  type        = number
  default     = 1
}

# Whether a firing alarm rolls a canary back automatically.
#
# DEFAULT false, deliberately. With this on, any deploy attempted while the
# fault injection is switched on will roll itself back -- correct behaviour, and
# baffling if you had forgotten the injection was still active. Turning it on is
# a demo step you take on purpose, not a surprise you inherit from an apply.
variable "canary_rollback_on_alarm" {
  description = "Roll a canary back automatically when a demo app alarm fires."
  type        = bool
  default     = false
}

# Phase 5.1. Off by default, and deliberately a separate switch from the gate's
# own MODEL_VERDICT_CAN_ACT.
#
# false -> the executor reads the verdict and LOGS which deployment config it
#          would have chosen, then deploys as it did before Phase 5. Safe to
#          apply and leave running against real pipeline traffic.
# true  -> the risk level picks the deployment config, and a verdict that cannot
#          be read stops the deploy.
#
# Whether the mechanism is armed is a CHOICE and choices should be explicit;
# whether an armed mechanism fails open or closed is a SAFETY PROPERTY and is
# not configurable at all. Same split as D-057.
variable "executor_enforces_verdict" {
  description = "Let the recorded risk level choose the deployment config, and stop the deploy when no verdict can be read."
  type        = bool
  default     = false
}
