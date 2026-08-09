# Account-wide monthly cost budget.
#
# Deliberately account-wide rather than filtered to this project's tags. A
# tag-filtered budget would miss anything created outside Terraform, which is
# exactly the spend most likely to surprise you. Cost allocation tags also have
# to be activated by hand in the Billing console and take ~24h to start
# populating, so a tag filter would silently report zero on day one.
#
# Notification-only. The hard budget action (which needs its own IAM role and
# can stop resources) lands in Phase 8, before the soak test authorises any
# standing cost.
#
# Cost: AWS provides the first two budgets per account free of charge.

resource "aws_budgets_budget" "monthly" {
  name         = "${var.project_name}-monthly"
  budget_type  = "COST"
  limit_amount = var.monthly_budget_usd
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  # Fires when spend has already crossed half the budget.
  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 50
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [var.budget_alert_email]
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 80
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [var.budget_alert_email]
  }

  # The forecast alert is the one that actually gives you time to react: it
  # fires when AWS projects month-end spend will exceed the limit, which is
  # usually days before the actual threshold is crossed.
  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_email_addresses = [var.budget_alert_email]
  }
}
