import frappe

def after_install():
    """Runs after app is installed on site."""
    frappe.logger().info("aretex_wa: after_install complete")

def before_uninstall():
    """Runs before app is removed from site."""
    frappe.logger().info("aretex_wa: before_uninstall complete")
