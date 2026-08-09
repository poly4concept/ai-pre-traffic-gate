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
variable "gate_mode" {
  description = "Operating mode of the deployment gate: shadow | advisory | enforcing."
  type        = string
  default     = "shadow"

  validation {
    condition     = contains(["shadow", "advisory", "enforcing"], var.gate_mode)
    error_message = "gate_mode must be one of: shadow, advisory, enforcing."
  }
}
