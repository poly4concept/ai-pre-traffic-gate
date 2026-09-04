# Phase 5.2 -- the escalation channel.
#
# D-009 settled the destination back in Phase 0: SNS with email subscribers, no
# Slack, because no Slack workspace is available. SNS is the right seam anyway.
# Amazon Q Developer in chat applications (the service formerly called AWS
# Chatbot) subscribes to an SNS topic, so adding a chat destination later means
# adding a subscriber here and changing nothing in the publisher.
#
# COST: effectively zero and no standing charge. SNS bills $0.50 per million
# publishes and the first thousand email notifications each month are free. This
# fires only when the gate halts, which is at most once per pipeline execution.
#
# NOT ENCRYPTED AT REST, and that is a decision rather than an omission --
# see D-069. The AWS-managed `aws/sns` KMS key does not exist in an account
# until SNS SSE is first used there, so looking up its ARN to scope an IAM
# statement fails on a clean apply. The payload is verdict reasoning and commit
# metadata, all of which is already in CloudWatch Logs. Encryption belongs with
# the rest of the Phase 7 hardening pass, against a customer-managed key.

resource "aws_sns_topic" "escalations" {
  name = "${var.project_name}-escalations"

  tags = {
    Purpose = "Deployment gate escalations"
  }
}

# Optional on purpose. An empty `escalation_email` leaves the topic with no
# subscribers, which is a working configuration: the gate publishes, the message
# goes nowhere, and nothing errors. That is the right default for a repo someone
# else might clone -- a Terraform apply should not send mail to an address
# hardcoded by its author.
#
# The consequence is worth stating because it bites once: after apply, AWS sends
# a confirmation email and the subscription stays `PendingConfirmation` until
# the link is clicked. An unconfirmed subscription receives nothing, and SNS
# reports the publish as a success. So a silent inbox after the first halt is
# more likely an unclicked link than a broken gate.
resource "aws_sns_topic_subscription" "escalation_email" {
  count = var.escalation_email == "" ? 0 : 1

  topic_arn = aws_sns_topic.escalations.arn
  protocol  = "email"
  endpoint  = var.escalation_email
}

output "escalation_topic_arn" {
  description = "SNS topic the gate publishes halts to."
  value       = aws_sns_topic.escalations.arn
}

output "escalation_subscription_status" {
  description = "Reminder that an email subscription is inert until confirmed."
  value       = var.escalation_email == "" ? "no subscriber configured; set escalation_email to receive halts" : "check ${var.escalation_email} and click the AWS confirmation link -- until then SNS reports publishes as successful and delivers nothing"
}
