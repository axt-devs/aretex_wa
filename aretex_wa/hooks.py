from . import __version__ as app_version

app_name = "aretex_wa"
app_title = "Aretex WA"
app_publisher = "Aretex"
app_description = "WhatsApp Automation Platform for Aretex HVAC"
app_email = "dev@aretex.com"
app_license = "MIT"
app_version = app_version

after_install = "aretex_wa.install.after_install"
before_uninstall = "aretex_wa.install.before_uninstall"

# Whitelisted API methods (accessible without login)
override_whitelisted_methods = {}

whitelist = [
    "aretex_wa.whatsapp_handler.receive_whatsapp_message",
    "aretex_wa.whatsapp_handler.receive_new_lead_flow_submission",
    "aretex_wa.whatsapp_handler.receive_support_flow_submission",
    "aretex_wa.whatsapp_handler.get_customer_history",
    "aretex_wa.whatsapp_handler.get_dashboard_stats",
]

scheduler_events = {
    "hourly_long": [
        "aretex_wa.api_background_tasks.check_sla_breaches"
    ],
    "daily": [
        "aretex_wa.api_background_tasks.send_daily_report",
        "aretex_wa.api_background_tasks.run_lead_reengagement"
    ],
    "cron": {
        "*/15 * * * *": [
            "aretex_wa.api_background_tasks.run_resource_scheduling"
        ]
    }
}