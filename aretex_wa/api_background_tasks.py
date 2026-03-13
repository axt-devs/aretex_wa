"""
api_background_tasks.py
Aretex WhatsApp Automation Platform v2.0
Four background scheduler jobs — all run automatically by Frappe.

Jobs:
  check_sla_breaches()      → every 3 hours  → 0 WhatsApp API calls
  run_lead_reengagement()   → daily midnight → 1 API call per follow-up (unavoidable)
  run_resource_scheduling() → every 15 min  → 0 WhatsApp API calls
  send_daily_report()       → daily midnight → 0 WhatsApp API calls
"""

import frappe
from datetime import datetime


# ─────────────────────────────────────────────────────────────────────────────
# JOB 1: CHECK SLA BREACHES (every 3 hours)
# ─────────────────────────────────────────────────────────────────────────────
def check_sla_breaches():
    """
    Scans open tickets for SLA breaches.
    Creates internal Frappe Tasks to alert the team.
    0 WhatsApp API calls — internal only.
    """
    try:
        now = frappe.utils.now_datetime()

        breached_tickets = frappe.get_all(
            "WA Service Request",
            filters={
                "status": ["in", ["Open", "In Progress"]],
                "sla_due_at": ["<", now]
            },
            fields=["name", "priority", "customer", "system_category", "assigned_to", "sla_due_at"]
        )

        for ticket in breached_tickets:
            # Check if we already created a breach task for this ticket
            already_logged = frappe.db.exists(
                "ToDo",
                {
                    "reference_type": "WA Service Request",
                    "reference_name": ticket["name"],
                    "description": ["like", "%SLA BREACH%"]
                }
            )
            if already_logged:
                continue

            # Create internal task (no WhatsApp message)
            todo = frappe.get_doc({
                "doctype": "ToDo",
                "status": "Open",
                "priority": "High" if ticket["priority"] == "HIGH" else "Medium",
                "reference_type": "WA Service Request",
                "reference_name": ticket["name"],
                "description": (
                    f"⚠️ SLA BREACH — Ticket {ticket['name']}\n"
                    f"Priority: {ticket['priority']}\n"
                    f"Category: {ticket.get('system_category', 'N/A')}\n"
                    f"SLA was due: {ticket['sla_due_at']}\n"
                    f"Assigned to: {ticket.get('assigned_to', 'Unassigned')}"
                )
            })
            todo.insert(ignore_permissions=True)

        frappe.db.commit()
        frappe.logger().info(f"check_sla_breaches: found {len(breached_tickets)} breaches")
        return {"success": True, "breaches_found": len(breached_tickets)}

    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "check_sla_breaches Error")
        return {"success": False, "error": str(e)}


# ─────────────────────────────────────────────────────────────────────────────
# JOB 2: LEAD RE-ENGAGEMENT (daily midnight)
# ─────────────────────────────────────────────────────────────────────────────
def run_lead_reengagement():
    """
    Sends follow-up WhatsApp templates to leads that haven't replied.
    State machine:
      none  → [7 days pass]  → send wa_followup_7d  → stage = after_7d
      after_7d → [14 days from creation] → send wa_followup_14d → stage = after_14d
      after_14d → [10 days since last outbound] → send wa_followup_last → stage = after_10d, status = Closed

    1 API call per lead per stage (unavoidable).
    """
    try:
        from aretex_wa.whatsapp_handler import _send_template, _log_message

        now = frappe.utils.now_datetime()
        sent_count = 0

        # ── STAGE 1: 7-day follow-up ──
        leads_7d = frappe.get_all(
            "WA Lead",
            filters={
                "status": "New",
                "reengagement_stage": "none",
                "created_at": ["<", frappe.utils.add_to_date(now, days=-7)]
            },
            fields=["name", "customer"]
        )

        for lead in leads_7d:
            wa_number = frappe.db.get_value("WA Customer", lead["customer"], "whatsapp_number")
            if not wa_number:
                continue

            sent = _send_template(wa_number, "wa_followup_7d")
            if sent:
                frappe.db.set_value("WA Lead", lead["name"], {
                    "reengagement_stage": "after_7d",
                    "last_outbound_at": frappe.utils.now()
                })
                _log_message(lead["customer"], "[wa_followup_7d sent]", "Outgoing", "Notification")
                sent_count += 1

        # ── STAGE 2: 14-day follow-up ──
        leads_14d = frappe.get_all(
            "WA Lead",
            filters={
                "status": "New",
                "reengagement_stage": "after_7d",
                "created_at": ["<", frappe.utils.add_to_date(now, days=-14)]
            },
            fields=["name", "customer"]
        )

        for lead in leads_14d:
            wa_number = frappe.db.get_value("WA Customer", lead["customer"], "whatsapp_number")
            if not wa_number:
                continue

            sent = _send_template(wa_number, "wa_followup_14d")
            if sent:
                frappe.db.set_value("WA Lead", lead["name"], {
                    "reengagement_stage": "after_14d",
                    "last_outbound_at": frappe.utils.now()
                })
                _log_message(lead["customer"], "[wa_followup_14d sent]", "Outgoing", "Notification")
                sent_count += 1

        # ── STAGE 3: Final follow-up (10 days after last outbound) ──
        leads_final = frappe.get_all(
            "WA Lead",
            filters={
                "status": "New",
                "reengagement_stage": "after_14d",
                "last_outbound_at": ["<", frappe.utils.add_to_date(now, days=-10)]
            },
            fields=["name", "customer"]
        )

        for lead in leads_final:
            wa_number = frappe.db.get_value("WA Customer", lead["customer"], "whatsapp_number")
            if not wa_number:
                continue

            sent = _send_template(wa_number, "wa_followup_last")
            if sent:
                frappe.db.set_value("WA Lead", lead["name"], {
                    "reengagement_stage": "after_10d",
                    "status": "Closed",
                    "last_outbound_at": frappe.utils.now()
                })
                _log_message(lead["customer"], "[wa_followup_last sent — sequence ended]", "Outgoing", "Notification")
                sent_count += 1

        frappe.db.commit()
        frappe.logger().info(f"run_lead_reengagement: sent {sent_count} follow-ups")
        return {"success": True, "sent": sent_count}

    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "run_lead_reengagement Error")
        return {"success": False, "error": str(e)}


# ─────────────────────────────────────────────────────────────────────────────
# JOB 3: RESOURCE SCHEDULING (every 15 minutes)
# ─────────────────────────────────────────────────────────────────────────────
def run_resource_scheduling():
    """
    Scans open tickets with no assigned resource.
    Attempts to find and book an available team member + slot.
    0 WhatsApp API calls — pure DB scheduling.
    """
    try:
        from aretex_wa.whatsapp_handler import _find_and_book_slot

        unassigned = frappe.get_all(
            "WA Service Request",
            filters={
                "status": "Open",
                "assigned_to": ["is", "not set"]
            },
            fields=["name", "resource_type", "system_category", "requested_datetime", "priority"],
            order_by="priority desc, creation asc",
            limit_page_length=50
        )

        assigned_count = 0
        for ticket in unassigned:
            member_name, slot = _find_and_book_slot(
                ticket.get("resource_type", "Technician"),
                ticket.get("system_category", "hvac"),
                ticket["name"],
                ticket.get("requested_datetime")
            )
            if member_name:
                assigned_count += 1

        frappe.logger().info(f"run_resource_scheduling: assigned {assigned_count}/{len(unassigned)} tickets")
        return {"success": True, "assigned": assigned_count, "total_unassigned": len(unassigned)}

    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "run_resource_scheduling Error")
        return {"success": False, "error": str(e)}


# ─────────────────────────────────────────────────────────────────────────────
# JOB 4: DAILY REPORT (daily midnight)
# ─────────────────────────────────────────────────────────────────────────────
def send_daily_report():
    """
    Computes daily summary stats and logs them (+ optionally emails admin).
    0 WhatsApp API calls.
    """
    try:
        now = frappe.utils.now_datetime()
        today_start = frappe.utils.get_datetime(frappe.utils.today())

        new_leads_today = frappe.db.count(
            "WA Lead",
            {"created_at": [">=", today_start]}
        )
        tickets_today = frappe.db.count(
            "WA Service Request",
            {"creation": [">=", today_start]}
        )
        open_high = frappe.db.count(
            "WA Service Request",
            {"status": "Open", "priority": "HIGH"}
        )
        sla_breached = frappe.db.count(
            "WA Service Request",
            {"status": "Open", "sla_due_at": ["<", now]}
        )
        total_customers = frappe.db.count("WA Customer")
        free_slots = frappe.db.count("WA Calendar Slot", {"status": "Free"})

        report_text = (
            f"=== Aretex WA Daily Report — {frappe.utils.today()} ===\n"
            f"New Leads Today:       {new_leads_today}\n"
            f"New Tickets Today:     {tickets_today}\n"
            f"Open HIGH Priority:    {open_high}\n"
            f"SLA Breached:          {sla_breached}\n"
            f"Total Customers:       {total_customers}\n"
            f"Free Calendar Slots:   {free_slots}\n"
        )

        frappe.logger().info(report_text)

        # Email report to admin (if configured)
        admin_email = frappe.conf.get("admin_email", "")
        if admin_email:
            frappe.sendmail(
                recipients=[admin_email],
                subject=f"Aretex WA Daily Report — {frappe.utils.today()}",
                message=report_text.replace("\n", "<br>")
            )

        return {"success": True, "report": report_text}

    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "send_daily_report Error")
        return {"success": False, "error": str(e)}
