"""
api_background_tasks.py  —  Aretex WhatsApp Automation Platform v2.0

Scheduler jobs:
  check_sla_breaches()      every 3 hours  — 0 WA API calls
  run_lead_reengagement()   daily midnight — 1 WA API call per follow-up
  run_resource_scheduling() every 15 min   — 0 WA API calls
  send_daily_report()       daily midnight — 0 WA API calls
"""

import frappe


# ---------------------------------------------------------------------------
# JOB 1 — SLA BREACHES  (every 3 hours)
# ---------------------------------------------------------------------------
def check_sla_breaches():
	"""Create internal ToDo alerts for overdue tickets. 0 WA API calls."""
	try:
		now = frappe.utils.now_datetime()
		breached = frappe.get_all(
			"WA Service Request",
			filters={
				"status": ["in", ["Open", "In Progress"]],
				"sla_due_at": ["<", now],
			},
			fields=["name", "priority", "customer", "system_category", "assigned_to", "sla_due_at"],
		)
		for ticket in breached:
			already_logged = frappe.db.exists(
				"ToDo",
				{
					"reference_type": "WA Service Request",
					"reference_name": ticket["name"],
					"description": ["like", "%SLA BREACH%"],
				},
			)
			if already_logged:
				continue
			frappe.get_doc({
				"doctype": "ToDo",
				"status": "Open",
				"priority": "High" if ticket["priority"] == "HIGH" else "Medium",
				"reference_type": "WA Service Request",
				"reference_name": ticket["name"],
				"description": (
					f"SLA BREACH — Ticket {ticket['name']}\n"
					f"Priority: {ticket['priority']}\n"
					f"Category: {ticket.get('system_category', 'N/A')}\n"
					f"SLA was due: {ticket['sla_due_at']}\n"
					f"Assigned to: {ticket.get('assigned_to', 'Unassigned')}"
				),
			}).insert(ignore_permissions=True)
		frappe.db.commit()
		frappe.logger().info(f"check_sla_breaches: {len(breached)} breach(es) found")
		return {"success": True, "breaches_found": len(breached)}
	except Exception as exc:
		frappe.log_error(frappe.get_traceback(), "check_sla_breaches Error")
		return {"success": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# JOB 2 — LEAD RE-ENGAGEMENT  (daily midnight)
# ---------------------------------------------------------------------------
def run_lead_reengagement():
	"""
	State machine:
	  none     → 7 days  → send wa_followup_7d  → after_7d
	  after_7d → 14 days → send wa_followup_14d → after_14d
	  after_14d→ 10 days → send wa_followup_last → after_10d / Closed
	1 WA API call per follow-up (unavoidable — template messages).
	"""
	try:
		from aretex_wa.whatsapp_handler import _send_template, _log_message

		now = frappe.utils.now_datetime()
		sent_count = 0

		# Stage 1 — 7-day follow-up
		for lead in frappe.get_all(
			"WA Lead",
			filters={
				"status": "New",
				"reengagement_stage": "none",
				"created_at": ["<", frappe.utils.add_to_date(now, days=-7)],
			},
			fields=["name", "customer"],
		):
			wa_number = frappe.db.get_value("WA Customer", lead["customer"], "whatsapp_number")
			if not wa_number:
				continue
			if _send_template(wa_number, "wa_followup_7d"):
				frappe.db.set_value("WA Lead", lead["name"], {
					"reengagement_stage": "after_7d",
					"last_outbound_at": frappe.utils.now(),
				})
				_log_message(lead["customer"], "[wa_followup_7d sent]", "Outgoing", "Notification")
				sent_count += 1

		# Stage 2 — 14-day follow-up
		for lead in frappe.get_all(
			"WA Lead",
			filters={
				"status": "New",
				"reengagement_stage": "after_7d",
				"created_at": ["<", frappe.utils.add_to_date(now, days=-14)],
			},
			fields=["name", "customer"],
		):
			wa_number = frappe.db.get_value("WA Customer", lead["customer"], "whatsapp_number")
			if not wa_number:
				continue
			if _send_template(wa_number, "wa_followup_14d"):
				frappe.db.set_value("WA Lead", lead["name"], {
					"reengagement_stage": "after_14d",
					"last_outbound_at": frappe.utils.now(),
				})
				_log_message(lead["customer"], "[wa_followup_14d sent]", "Outgoing", "Notification")
				sent_count += 1

		# Stage 3 — final follow-up (10 days after last outbound)
		for lead in frappe.get_all(
			"WA Lead",
			filters={
				"status": "New",
				"reengagement_stage": "after_14d",
				"last_outbound_at": ["<", frappe.utils.add_to_date(now, days=-10)],
			},
			fields=["name", "customer"],
		):
			wa_number = frappe.db.get_value("WA Customer", lead["customer"], "whatsapp_number")
			if not wa_number:
				continue
			if _send_template(wa_number, "wa_followup_last"):
				frappe.db.set_value("WA Lead", lead["name"], {
					"reengagement_stage": "after_10d",
					"status": "Closed",
					"last_outbound_at": frappe.utils.now(),
				})
				_log_message(lead["customer"], "[wa_followup_last sent]", "Outgoing", "Notification")
				sent_count += 1

		frappe.db.commit()
		frappe.logger().info(f"run_lead_reengagement: {sent_count} message(s) sent")
		return {"success": True, "sent": sent_count}

	except Exception as exc:
		frappe.log_error(frappe.get_traceback(), "run_lead_reengagement Error")
		return {"success": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# JOB 3 — RESOURCE SCHEDULING  (every 15 minutes)
# ---------------------------------------------------------------------------
def run_resource_scheduling():
	"""Auto-assign unassigned open tickets to available team members. 0 WA API calls."""
	try:
		from aretex_wa.whatsapp_handler import _find_and_book_slot

		unassigned = frappe.get_all(
			"WA Service Request",
			filters={"status": "Open", "assigned_to": ["is", "not set"]},
			fields=["name", "resource_type", "system_category", "requested_datetime", "priority"],
			order_by="priority desc, creation asc",
			limit_page_length=50,
		)
		assigned_count = 0
		for ticket in unassigned:
			member_name, _ = _find_and_book_slot(
				ticket.get("resource_type", "Technician"),
				ticket.get("system_category", "hvac"),
				ticket["name"],
				ticket.get("requested_datetime"),
			)
			if member_name:
				assigned_count += 1

		frappe.logger().info(
			f"run_resource_scheduling: {assigned_count}/{len(unassigned)} ticket(s) assigned"
		)
		return {"success": True, "assigned": assigned_count, "total_unassigned": len(unassigned)}

	except Exception as exc:
		frappe.log_error(frappe.get_traceback(), "run_resource_scheduling Error")
		return {"success": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# JOB 4 — DAILY REPORT  (daily midnight)
# ---------------------------------------------------------------------------
def send_daily_report():
	"""Log daily summary and email admin. 0 WA API calls."""
	try:
		now = frappe.utils.now_datetime()
		today_start = frappe.utils.get_datetime(frappe.utils.today())

		stats = {
			"new_leads_today": frappe.db.count("WA Lead", {"created_at": [">=", today_start]}),
			"tickets_today": frappe.db.count("WA Service Request", {"creation": [">=", today_start]}),
			"open_high": frappe.db.count("WA Service Request", {"status": "Open", "priority": "HIGH"}),
			"sla_breached": frappe.db.count(
				"WA Service Request", {"status": "Open", "sla_due_at": ["<", now]}
			),
			"total_customers": frappe.db.count("WA Customer"),
			"free_slots": frappe.db.count("WA Calendar Slot", {"status": "Free"}),
		}

		report_text = (
			f"Aretex WA Daily Report — {frappe.utils.today()}\n"
			f"New Leads Today:     {stats['new_leads_today']}\n"
			f"New Tickets Today:   {stats['tickets_today']}\n"
			f"Open HIGH Priority:  {stats['open_high']}\n"
			f"SLA Breached:        {stats['sla_breached']}\n"
			f"Total Customers:     {stats['total_customers']}\n"
			f"Free Calendar Slots: {stats['free_slots']}\n"
		)

		frappe.logger().info(report_text)

		admin_email = frappe.conf.get("admin_email", "")
		if admin_email:
			frappe.sendmail(
				recipients=[admin_email],
				subject=f"Aretex WA Daily Report — {frappe.utils.today()}",
				message=report_text.replace("\n", "<br>"),
			)

		return {"success": True, "report": report_text}

	except Exception as exc:
		frappe.log_error(frappe.get_traceback(), "send_daily_report Error")
		return {"success": False, "error": str(exc)}
