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

# --- Demo app -------------------------------------------------------------

output "demo_app_function_name" {
  description = "Demo app Lambda function name."
  value       = aws_lambda_function.demo_app.function_name
}

output "demo_app_alias_arn" {
  description = "ARN of the `live` alias. This is what CodeDeploy shifts traffic on."
  value       = aws_lambda_alias.live.arn
}

output "demo_app_function_url" {
  description = "Function URL for the `live` alias. Requires SigV4 -- see demo_app_curl."
  value       = aws_lambda_function_url.demo_app_live.function_url
}

# --- Canary and gate ------------------------------------------------------

output "codedeploy_app_name" {
  description = "CodeDeploy application for the demo app."
  value       = aws_codedeploy_app.demo_app.name
}

output "codedeploy_deployment_group" {
  description = "Deployment group holding the traffic-shifting policy."
  value       = aws_codedeploy_deployment_group.demo_app.deployment_group_name
}

output "canary_shape" {
  description = "Traffic-shifting shape currently configured."
  value       = "${var.canary_percentage}% for ${var.canary_interval_minutes} min, then the remainder"
}

output "gate_function_name" {
  description = "Phase 1 gate stub Lambda. Holds no deploy permissions."
  value       = aws_lambda_function.gate_stub.function_name
}

output "gate_decision" {
  description = "Hardcoded verdict the gate stub will return."
  value       = var.gate_decision
}

# Invoking the gate directly is the fastest way to see the fail-closed
# behaviour, and it needs no pipeline. Worth emitting because the interesting
# test is the one where you break the config on purpose.
output "gate_invoke" {
  description = "Invoke the gate stub and print its verdict."
  value = join(" ", [
    "aws lambda invoke --function-name ${aws_lambda_function.gate_stub.function_name}",
    "--cli-binary-format raw-in-base64-out --payload '{}' /dev/stdout",
  ])
}

# The URL is useless without a signed request, and an unsigned curl returns a
# bare 403 with no hint about why. Emitting the working command removes a
# predictable five minutes of confusion, including on stage.
output "demo_app_curl" {
  description = "Ready-to-run signed request against the demo app."
  value = join(" ", [
    "curl --aws-sigv4 'aws:amz:${data.aws_region.current.region}:lambda'",
    "--user \"$AWS_ACCESS_KEY_ID:$AWS_SECRET_ACCESS_KEY\"",
    "-H \"x-amz-security-token: $AWS_SESSION_TOKEN\"",
    aws_lambda_function_url.demo_app_live.function_url,
  ])
}
