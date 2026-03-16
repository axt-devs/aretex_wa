"""
whatsapp_handler.py
Aretex WhatsApp Automation Platform v2.0
Handles all inbound WhatsApp messages and Flow submissions.

Endpoints:
  - receive_whatsapp_message       (POST - Meta webhook)
  - receive_new_lead_flow_submission  (POST - NewLeadFlow submit)
  - receive_support_flow_submission   (POST - SupportFlow submit)
  - get_customer_history           (GET  - Admin Desk)
  - get_dashboard_stats            (GET  - Admin Desk)
"""

import frappe
import json
import hmac
import hashlib
import requests
from datetime import datetime


# ─────────────────────────────────────────────────────────────────────────────
# RATE LIMITING (simple in-memory per-number cooldown)
# ─────────────────────────────────────────────────────────────────────────────
_rate_cache = {}
RATE_LIMIT_SECONDS = 2  # minimum seconds between messages from same number


def _is_rate_limited(phone_number):
    now = datetime.now().timestamp()
    last = _rate_cache.get(phone_number, 0)
    if now - last < RATE_LIMIT_SECONDS:
        return True
    _rate_cache[phone_number] = now
    return False


# ─────────────────────────────────────────────────────────────────────────────
# HMAC SIGNATURE VERIFICATION
# ─────────────────────────────────────────────────────────────────────────────
def _verify_signature(request_body_bytes, signature_header):
    """Verify X-Hub-Signature-256 from Meta."""
    app_secret = frappe.conf.get("whatsapp_app_secret", "")
    if not app_secret:
        return True  # skip if not configured yet (dev mode)
    expected = "sha256=" + hmac.new(
        app_secret.encode("utf-8"),
        request_body_bytes,
        hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature_header or "")



# ─────────────────────────────────────────────────────────────────────────────
# SEND A WHATSAPP FLOW
# ─────────────────────────────────────────────────────────────────────────────
def _send_flow(to_number, flow_id, flow_token="unused"):
    """Send a WhatsApp Flow message. Counts as 1 outbound API call."""
    access_token = frappe.conf.get("whatsapp_access_token", "")
    phone_number_id = frappe.conf.get("whatsapp_phone_number_id", "")

    if not access_token or not phone_number_id:
        frappe.log_error("WhatsApp credentials not configured", "WA Config Error")
        return False

    url = f"https://graph.facebook.com/v19.0/{phone_number_id}/messages"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json"
    }
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to_number,
        "type": "interactive",
        "interactive": {
            "type": "flow",
            "body": {"text": "Please fill in the form below to continue."},
            "action": {
                "name": "flow",
                "parameters": {
                    "flow_message_version": "3",
                    "flow_token": flow_token,
                    "flow_id": flow_id,
                    "flow_cta": "Open Form",
                    "flow_action": "navigate",
                    "flow_action_payload": {
                        "screen": "WELCOME"
                    }
                }
            }
        }
    }
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=10)
        resp.raise_for_status()
        return True
    except Exception as e:
        frappe.log_error(str(e), "WA Send Flow Error")
        return False


# ─────────────────────────────────────────────────────────────────────────────
# SEND A PLAIN TEXT MESSAGE
# ─────────────────────────────────────────────────────────────────────────────
def _send_text(to_number, message_text):
    """Send a plain text WhatsApp message. Counts as 1 outbound API call."""
    access_token = frappe.conf.get("whatsapp_access_token", "")
    phone_number_id = frappe.conf.get("whatsapp_phone_number_id", "")

    if not access_token or not phone_number_id:
        frappe.log_error("WhatsApp credentials not configured", "WA Config Error")
        return False

    url = f"https://graph.facebook.com/v19.0/{phone_number_id}/messages"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json"
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": to_number,
        "type": "text",
        "text": {"body": message_text}
    }
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=10)
        resp.raise_for_status()
        return True
    except Exception as e:
        frappe.log_error(str(e), "WA Send Text Error")
        return False


# ─────────────────────────────────────────────────────────────────────────────
# SEND A TEMPLATE MESSAGE (for re-engagement)
# ─────────────────────────────────────────────────────────────────────────────
def _send_template(to_number, template_name, language_code="en_US"):
    """Send an approved WhatsApp template. Counts as 1 API call."""
    access_token = frappe.conf.get("whatsapp_access_token", "")
    phone_number_id = frappe.conf.get("whatsapp_phone_number_id", "")

    if not access_token or not phone_number_id:
        frappe.log_error("WhatsApp credentials not configured", "WA Config Error")
        return False

    url = f"https://graph.facebook.com/v19.0/{phone_number_id}/messages"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json"
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": to_number,
        "type": "template",
        "template": {
            "name": template_name,
            "language": {"code": language_code}
        }
    }
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=10)
        resp.raise_for_status()
        return True
    except Exception as e:
        frappe.log_error(str(e), f"WA Template Send Error ({template_name})")
        return False


# ─────────────────────────────────────────────────────────────────────────────
# CUSTOMER LOOKUP / CREATION
# ─────────────────────────────────────────────────────────────────────────────
def find_or_create_customer(whatsapp_number):
    """
    Look up a WA Customer by WhatsApp number.
    Returns (customer_doc, is_existing) where is_existing = True if
    the customer already existed as an existing client.
    Pure DB — 0 API calls.
    """
    existing = frappe.db.get_value(
        "WA Customer",
        {"whatsapp_number": whatsapp_number},
        ["name", "is_existing_customer", "customer_name"],
        as_dict=True
    )

    if existing:
        # Update last contact timestamp
        frappe.db.set_value("WA Customer", existing["name"], "last_contact_at", frappe.utils.now())
        return existing, bool(existing.get("is_existing_customer"))

    # Create new customer record
    doc = frappe.get_doc({
        "doctype": "WA Customer",
        "whatsapp_number": whatsapp_number,
        "is_existing_customer": 0,
        "created_at": frappe.utils.now(),
        "last_contact_at": frappe.utils.now()
    })
    doc.insert(ignore_permissions=True)
    frappe.db.commit()

    return doc.as_dict(), False


# ─────────────────────────────────────────────────────────────────────────────
# ROUTE: SEND THE CORRECT FLOW
# ─────────────────────────────────────────────────────────────────────────────
def _send_appropriate_flow(whatsapp_number, is_existing_customer):
    """
    Send NewLeadFlow or SupportFlow depending on customer type.
    1 outbound API call.
    """
    if is_existing_customer:
        flow_id = frappe.conf.get("wa_support_flow_id", "")
    else:
        flow_id = frappe.conf.get("wa_new_lead_flow_id", "")

    if not flow_id:
        frappe.log_error(
            f"Flow ID not configured for {'existing' if is_existing_customer else 'new'} customer",
            "WA Flow Config Error"
        )
        return False

    return _send_flow(whatsapp_number, flow_id)


# ─────────────────────────────────────────────────────────────────────────────
# MARK A LEAD AS REPLIED (stops re-engagement)
# ─────────────────────────────────────────────────────────────────────────────
def _mark_lead_replied(customer_name):
    """
    When a customer sends any message, stop their re-engagement sequence.
    Pure DB — 0 API calls.
    """
    leads = frappe.get_all(
        "WA Lead",
        filters={
            "customer": customer_name,
            "status": ["!=", "Closed"],
            "reengagement_stage": ["!=", "done"]
        },
        fields=["name"]
    )
    for lead in leads:
        frappe.db.set_value("WA Lead", lead["name"], {
            "reengagement_stage": "done",
            "last_inbound_at": frappe.utils.now()
        })
    if leads:
        frappe.db.commit()


# ─────────────────────────────────────────────────────────────────────────────
# ATTACH MEDIA TO OPEN TICKET
# ─────────────────────────────────────────────────────────────────────────────
def _attach_media_to_ticket(customer_name, wa_message_id, media_type):
    """
    When a customer sends media, attach it silently to their most recent open ticket.
    1 API call for media download.
    """
    # Find the most recent open ticket for this customer
    ticket = frappe.db.get_value(
        "WA Service Request",
        filters={
            "customer": customer_name,
            "status": ["in", ["Open", "In Progress"]]
        },
        fieldname="name",
        order_by="creation desc"
    )

    if not ticket:
        return  # No open ticket — silently ignore

    doc = frappe.get_doc({
        "doctype": "WA Ticket Media",
        "ticket": ticket,
        "media_type": media_type.capitalize(),
        "whatsapp_media_id": wa_message_id,
        "uploaded_at": frappe.utils.now()
    })
    doc.insert(ignore_permissions=True)
    frappe.db.commit()


# ─────────────────────────────────────────────────────────────────────────────
# PRIORITY CALCULATION
# ─────────────────────────────────────────────────────────────────────────────
def compute_priority(scope, system_category, request_type):
    """
    Returns "HIGH", "MEDIUM", or "LOW" based on Aretex priority matrix.
    0 API calls.
    """
    if request_type in ["query", "disclosed"]:
        return "LOW"

    if scope == "full_system":
        return "HIGH"

    if scope == "specific_area":
        if system_category in ["hvac", "security"]:
            return "HIGH"
        return "MEDIUM"

    # specific_device
    return "MEDIUM"


# ─────────────────────────────────────────────────────────────────────────────
# RESOURCE TYPE DECISION
# ─────────────────────────────────────────────────────────────────────────────
def decide_resource_type(scope, system_category):
    """
    Returns "Engineer" or "Technician".
    0 API calls.
    """
    if scope == "full_system":
        return "Engineer"
    if system_category in ["hvac", "security"]:
        return "Engineer"
    return "Technician"


# ─────────────────────────────────────────────────────────────────────────────
# FIND AND BOOK A CALENDAR SLOT
# ─────────────────────────────────────────────────────────────────────────────
def _find_and_book_slot(resource_type, system_category, ticket_name, requested_datetime=None):
    """
    Find an available team member + calendar slot.
    Book it and return (member_name, slot_name) or (None, None).
    0 API calls.
    """
    # Find team members with matching role and skill
    members = frappe.get_all(
        "WA Team Member",
        filters={"role": resource_type, "active": 1},
        fields=["name", "name_of_member"]
    )

    for member in members:
        # Check if member has the required skill
        has_skill = frappe.db.exists(
            "WA Team Member Skill",
            {"parent": member["name"], "skill": system_category}
        )
        if not has_skill:
            continue

        # Find a free slot
        slot_filters = {
            "team_member": member["name"],
            "status": "Free"
        }
        if requested_datetime:
            try:
                req_date = str(requested_datetime)[:10]
                slot_filters["date"] = req_date
            except Exception:
                pass

        slot = frappe.db.get_value(
            "WA Calendar Slot",
            slot_filters,
            ["name", "date", "start_time"],
            as_dict=True,
            order_by="date asc, start_time asc"
        )

        if slot:
            # Book the slot
            frappe.db.set_value("WA Calendar Slot", slot["name"], {
                "status": "Busy",
                "ticket": ticket_name
            })
            # Update ticket with assignment
            frappe.db.set_value("WA Service Request", ticket_name, {
                "assigned_to": member["name"],
                "scheduled_slot": slot["name"],
                "resource_type": resource_type
            })
            frappe.db.commit()
            return member["name_of_member"], slot

    return None, None


# ─────────────────────────────────────────────────────────────────────────────
# LOG MESSAGE
# ─────────────────────────────────────────────────────────────────────────────
def _log_message(customer_name, text, direction, msg_type="Text", wa_message_id=""):
    """Log a message to WA Message Log. Pure DB."""
    frappe.get_doc({
        "doctype": "WA Message Log",
        "customer": customer_name,
        "message_text": text,
        "direction": direction,
        "timestamp": frappe.utils.now(),
        "message_type": msg_type,
        "wa_message_id": wa_message_id
    }).insert(ignore_permissions=True)


# ─────────────────────────────────────────────────────────────────────────────
# ENDPOINT 1: RECEIVE WHATSAPP MESSAGE (Meta Webhook)
# ─────────────────────────────────────────────────────────────────────────────
@frappe.whitelist(allow_guest=True)
def receive_whatsapp_message(**kwargs):
    """
    POST   → Processes inbound messages (texts, media, statuses)
    GET    → Webhook verification handshake from Meta
    API calls: 0 inbound + 1 outbound Flow send (per new session)
    """
    request = frappe.request

    # ── WEBHOOK VERIFICATION (GET) ──
    if request.method == "GET":
        params = frappe.form_dict
        mode = params.get("hub.mode")
        token = params.get("hub.verify_token")
        challenge = params.get("hub.challenge")
        stored_token = frappe.conf.get("whatsapp_verify_token", "")

        if mode == "subscribe" and token == stored_token:
    frappe.response["type"] = "txt"
    frappe.response["txt"] = challenge   
    return
        frappe.response.update({"http_status_code": 403})
        return "Forbidden"

    # ── INBOUND MESSAGE PROCESSING (POST) ──
    try:
        # 1. Verify HMAC signature
        sig = request.headers.get("X-Hub-Signature-256", "")
        body_bytes = request.get_data()
        if not _verify_signature(body_bytes, sig):
            frappe.response.update({"http_status_code": 401})
            return {"error": "Invalid signature"}

        # 2. Parse body
        body = json.loads(body_bytes)
        entry = body.get("entry", [{}])[0]
        changes = entry.get("changes", [{}])[0]
        value = changes.get("value", {})
        messages = value.get("messages", [])

        if not messages:
            # Status update or other — ignore
            return {"status": "ok"}

        msg = messages[0]
        from_number = msg.get("from", "")
        msg_type = msg.get("type", "text")
        wa_message_id = msg.get("id", "")

        # 3. Rate limit check
        if _is_rate_limited(from_number):
            return {"status": "rate_limited"}

        # 4. Find or create customer
        customer, is_existing = find_or_create_customer(from_number)
        customer_name = customer.get("name") or customer.name

        # 5. Stop re-engagement if customer replied
        _mark_lead_replied(customer_name)

        # 6. Handle media — attach silently, no reply
        if msg_type in ["image", "video"]:
            media_id = (msg.get("image") or msg.get("video") or {}).get("id", "")
            _attach_media_to_ticket(customer_name, media_id or wa_message_id, msg_type)
            _log_message(customer_name, f"[{msg_type} received]", "Incoming", "Media", wa_message_id)
            return {"status": "media_attached"}

        # 7. For text messages — send appropriate Flow (1 outbound API call)
        if msg_type == "text":
            text_body = msg.get("text", {}).get("body", "")
            _log_message(customer_name, text_body, "Incoming", "Text", wa_message_id)

            sent = _send_appropriate_flow(from_number, is_existing)
            flow_type = "SupportFlow" if is_existing else "NewLeadFlow"
            _log_message(customer_name, f"[{flow_type} sent]", "Outgoing", "Flow")

            return {"status": "flow_sent", "flow": flow_type, "success": sent}

        return {"status": "unhandled_type", "type": msg_type}

    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "WA Webhook Error")
        return {"error": str(e)}


# ─────────────────────────────────────────────────────────────────────────────
# ENDPOINT 2: NEW LEAD FLOW SUBMISSION
# ─────────────────────────────────────────────────────────────────────────────
@frappe.whitelist(allow_guest=True)
def receive_new_lead_flow_submission(**kwargs):
    """
    Called by Meta when a customer submits NewLeadFlow.
    Creates WA Lead record + sends 1 confirmation message.
    API calls: 1 outbound (confirmation)
    """
    try:
        data = frappe.form_dict

        whatsapp_number = data.get("whatsapp_number", "")
        info_type = data.get("info_type", "")
        wants_more_info = str(data.get("wants_more_info", "false")).lower() == "true"
        lead_type = data.get("lead_type", "none")
        preferred_datetime = data.get("preferred_datetime", "")
        lead_name = data.get("name", "")
        lead_email = data.get("email", "")
        notes = data.get("notes", "")

        if not whatsapp_number:
            return {"success": False, "error": "Missing whatsapp_number"}

        # Find or create customer
        customer, _ = find_or_create_customer(whatsapp_number)
        customer_name = customer.get("name") or customer.name

        # Create WA Lead
        lead_doc = frappe.get_doc({
            "doctype": "WA Lead",
            "customer": customer_name,
            "info_type": info_type,
            "wants_more_info": 1 if wants_more_info else 0,
            "lead_type": lead_type,
            "preferred_datetime": preferred_datetime or None,
            "name_of_lead": lead_name,
            "email_of_lead": lead_email,
            "notes": notes,
            "status": "New",
            "reengagement_stage": "none",
            "created_at": frappe.utils.now()
        })
        lead_doc.insert(ignore_permissions=True)
        frappe.db.commit()

        # Log incoming flow submission
        _log_message(customer_name, "[NewLeadFlow submitted]", "Incoming", "Flow")

        # Build confirmation message
        if lead_type == "site_survey":
            action_line = f"✅ Site survey request received."
            if preferred_datetime:
                action_line += f" Preferred time: {preferred_datetime[:10]}"
        elif lead_type == "callback":
            action_line = "✅ Callback request received. Our team will reach you soon."
        else:
            action_line = "✅ Thank you for your interest!"

        confirm_msg = (
            f"Hi{' ' + lead_name if lead_name else ''}! 👋\n\n"
            f"{action_line}\n\n"
            f"Our team at Aretex HVAC will get in touch with you shortly.\n\n"
            f"Reference: {lead_doc.name}"
        )

        # Send confirmation (1 outbound API call)
        _send_text(whatsapp_number, confirm_msg)
        _log_message(customer_name, confirm_msg, "Outgoing", "Notification")

        return {"success": True, "lead": lead_doc.name}

    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "WA New Lead Flow Error")
        return {"success": False, "error": str(e)}


# ─────────────────────────────────────────────────────────────────────────────
# ENDPOINT 3: SUPPORT FLOW SUBMISSION
# ─────────────────────────────────────────────────────────────────────────────
@frappe.whitelist(allow_guest=True)
def receive_support_flow_submission(**kwargs):
    """
    Called by Meta when a customer submits SupportFlow.
    Creates WA Service Request, computes priority, books a slot,
    sends 1 combined confirmation message.
    API calls: 1 outbound (confirmation)
    """
    try:
        data = frappe.form_dict

        whatsapp_number = data.get("whatsapp_number", "")
        request_type = data.get("request_type", "issue")
        description = data.get("description", "")
        scope = data.get("scope", "specific_device")
        system_category = data.get("system_category", "hvac")
        location = data.get("location", "")
        fault_type = data.get("fault_type", "")
        requested_datetime = data.get("requested_datetime", "")
        notes = data.get("notes", "")

        if not whatsapp_number:
            return {"success": False, "error": "Missing whatsapp_number"}

        # Find or create customer
        customer, _ = find_or_create_customer(whatsapp_number)
        customer_name = customer.get("name") or customer.name

        # Compute priority and resource type
        priority = compute_priority(scope, system_category, request_type)
        resource_type = decide_resource_type(scope, system_category)

        # Create WA Service Request
        ticket_doc = frappe.get_doc({
            "doctype": "WA Service Request",
            "customer": customer_name,
            "request_type": request_type.capitalize(),
            "description": description,
            "scope": scope,
            "system_category": system_category,
            "location": location,
            "fault_type": fault_type,
            "notes": notes,
            "priority": priority,
            "status": "Open",
            "resource_type": resource_type,
            "requested_datetime": requested_datetime or None,
            "sla_due_at": _compute_sla_due(priority)
        })
        ticket_doc.insert(ignore_permissions=True)
        frappe.db.commit()

        # Try to book a slot
        assigned_member, slot = _find_and_book_slot(
            resource_type, system_category, ticket_doc.name, requested_datetime
        )

        # Log incoming
        _log_message(customer_name, "[SupportFlow submitted]", "Incoming", "Flow")

        # Build combined confirmation (1 outbound API call)
        priority_emoji = {"HIGH": "🔴", "MEDIUM": "🟠", "LOW": "🟡"}.get(priority, "⚪")

        if assigned_member and slot:
            slot_info = f"📅 Scheduled: {slot.get('date', '')} at {str(slot.get('start_time', ''))[:5]}\n👤 Assigned to: {assigned_member}"
        else:
            slot_info = "📅 Our scheduling team will confirm your appointment time shortly."

        confirm_msg = (
            f"✅ Support ticket raised!\n\n"
            f"Ticket: {ticket_doc.name}\n"
            f"Priority: {priority_emoji} {priority}\n"
            f"Category: {system_category.upper()} — {location}\n"
            f"Resource: {resource_type}\n\n"
            f"{slot_info}\n\n"
            f"You can send photos/videos of the issue in this chat — they'll be auto-attached.\n\n"
            f"SLA deadline: {str(ticket_doc.sla_due_at)[:10] if ticket_doc.sla_due_at else 'TBD'}"
        )

        _send_text(whatsapp_number, confirm_msg)
        _log_message(customer_name, confirm_msg, "Outgoing", "Notification")

        return {"success": True, "ticket": ticket_doc.name, "priority": priority}

    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "WA Support Flow Error")
        return {"success": False, "error": str(e)}


def _compute_sla_due(priority):
    """Compute SLA deadline based on priority."""
    import frappe.utils
    now = frappe.utils.now_datetime()
    if priority == "HIGH":
        return frappe.utils.add_to_date(now, hours=24)
    elif priority == "MEDIUM":
        return frappe.utils.add_to_date(now, days=7)
    else:
        return frappe.utils.add_to_date(now, days=14)


# ─────────────────────────────────────────────────────────────────────────────
# ENDPOINT 4: GET CUSTOMER HISTORY (Admin)
# ─────────────────────────────────────────────────────────────────────────────
@frappe.whitelist()
def get_customer_history(whatsapp_number=None, limit=50):
    """
    Returns message history for a customer.
    GET — Admin Desk only. 0 API calls.
    """
    if not whatsapp_number:
        return {"success": False, "error": "whatsapp_number required"}

    customer = frappe.db.get_value(
        "WA Customer",
        {"whatsapp_number": whatsapp_number},
        "name"
    )
    if not customer:
        return {"success": False, "error": "Customer not found"}

    logs = frappe.get_all(
        "WA Message Log",
        filters={"customer": customer},
        fields=["message_text", "direction", "timestamp", "message_type"],
        order_by="timestamp desc",
        limit_page_length=int(limit)
    )

    tickets = frappe.get_all(
        "WA Service Request",
        filters={"customer": customer},
        fields=["name", "priority", "status", "system_category", "creation"],
        order_by="creation desc",
        limit_page_length=10
    )

    return {
        "success": True,
        "customer": customer,
        "messages": logs,
        "tickets": tickets
    }


# ─────────────────────────────────────────────────────────────────────────────
# ENDPOINT 5: DASHBOARD STATS (Admin)
# ─────────────────────────────────────────────────────────────────────────────
@frappe.whitelist()
def get_dashboard_stats():
    """
    Returns summary statistics for the admin dashboard.
    GET — Admin Desk only. 0 API calls.
    """
    total_customers = frappe.db.count("WA Customer")
    new_leads = frappe.db.count("WA Lead", {"status": "New"})
    open_tickets = frappe.db.count("WA Service Request", {"status": "Open"})
    high_priority = frappe.db.count("WA Service Request", {"status": "Open", "priority": "HIGH"})
    sla_breached = frappe.db.count(
        "WA Service Request",
        {
            "status": "Open",
            "sla_due_at": ["<", frappe.utils.now()]
        }
    )
    free_slots = frappe.db.count("WA Calendar Slot", {"status": "Free"})

    return {
        "success": True,
        "stats": {
            "total_customers": total_customers,
            "new_leads": new_leads,
            "open_tickets": open_tickets,
            "high_priority_tickets": high_priority,
            "sla_breached": sla_breached,
            "free_calendar_slots": free_slots
        }
    }
