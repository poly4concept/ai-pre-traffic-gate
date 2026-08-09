output "account_id" {
  description = "Account this stack is deployed into. Sanity check against the intended personal account."
  value       = data.aws_caller_identity.current.account_id
}

output "region" {
  description = "Region this stack is deployed into."
  value       = data.aws_region.current.region
}

output "gate_mode" {
  description = "Operating mode the gate is configured for."
  value       = var.gate_mode
}
