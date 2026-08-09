output "state_bucket_name" {
  description = "S3 bucket holding Terraform state. Must match the `bucket` value in infra/personal/backend.tf."
  value       = aws_s3_bucket.tfstate.id
}

output "budget_name" {
  description = "Name of the account-wide monthly cost budget."
  value       = aws_budgets_budget.monthly.name
}

output "next_step" {
  description = "What to do after this stack applies."
  # Budget EMAIL subscribers receive alerts directly - there is no opt-in
  # confirmation step. That applies to SNS topic subscriptions, not to the
  # EMAIL subscriber type used here. Do not tell the operator to wait for a
  # confirmation link that will never arrive.
  value = "Budget alerts go to ${var.budget_alert_email} (no confirmation needed). Next: cd ../personal && terraform init"
}
