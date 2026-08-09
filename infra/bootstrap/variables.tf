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

variable "monthly_budget_usd" {
  description = "Account-wide monthly cost budget in USD. Notification-only; the hard budget action arrives in Phase 8."
  type        = string
  default     = "20"
}

variable "budget_alert_email" {
  description = "Email address that receives budget alerts. Must be confirmed by clicking the link AWS emails you."
  type        = string

  validation {
    condition     = can(regex("^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$", var.budget_alert_email))
    error_message = "budget_alert_email must be a valid email address."
  }
}
