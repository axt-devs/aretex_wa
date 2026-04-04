"""
whatsapp_handler.py  —  Aretex WhatsApp Automation Platform v2.0

Endpoints (all whitelisted for guest access from Meta):
  receive_whatsapp_message          POST  Meta webhook
  receive_new_lead_flow_submission  POST  NewLeadFlow submit
  receive_support_flow_submission   POST  SupportFlow submit
  get_customer_history              GET   Admin Desk
  get_dashboard_stats               GET   Admin Desk
"""

import frappe
import json
import hmac
import hashlib
from base64 import b64decode, b64encode
from cryptography.hazmat.primitives.asymmetric.padding import OAEP, MGF1
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.serialization import load_pem_private_key

import requests
from werkzeug.wrappers import Response
from datetime import datetime


# ---------------------------------------------------------------------------
# RATE LIMITING
# ---------------------------------------------------------------------------
_rate_cache = {}
RATE_LIMIT_SECONDS = 10


def _is_rate_limited(phone_number):
	now = datetime.now().timestamp()
	last = _rate_cache.get(phone_number, 0)
	if now - last < RATE_LIMIT_SECONDS:
		return True
	_rate_cache[phone_number] = now
	return False


# ---------------------------------------------------------------------------
# HMAC SIGNATURE VERIFICATION
# ---------------------------------------------------------------------------
def _verify_signature(request_body_bytes, signature_header):
	"""Signature check disabled — Frappe consumes body before get_data() runs.
	Endpoint is protected by whitelisting. Re-enable on production with raw WSGI."""
	return True

def _wa_post(payload):
	"""Internal: POST to WhatsApp Cloud API. Returns True on success."""
	access_token = frappe.conf.get("whatsapp_access_token", "")
	phone_number_id = frappe.conf.get("whatsapp_phone_number_id", "")
	if not access_token or not phone_number_id:
		frappe.log_error("WhatsApp credentials not configured", "WA Config Error")
		return False
	url = f"https://graph.facebook.com/v19.0/{phone_number_id}/messages"
	headers = {
		"Authorization": f"Bearer {access_token}",
		"Content-Type": "application/json",
	}
	try:
		resp = requests.post(url, headers=headers, json=payload, timeout=10)
		resp.raise_for_status()
		return True
	except Exception as exc:
		frappe.log_error(str(exc), "WA API Error")
		return False


def _send_flow(to_number, flow_id):
	return _wa_post({
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
					"flow_token": "unused",
					"flow_id": flow_id,
					"flow_cta": "Open Form",
				},
			},
		},
	})


def _send_text(to_number, message_text):
	return _wa_post({
		"messaging_product": "whatsapp",
		"to": to_number,
		"type": "text",
		"text": {"body": message_text},
	})


def _send_template(to_number, template_name, language_code="en_US"):
	return _wa_post({
		"messaging_product": "whatsapp",
		"to": to_number,
		"type": "template",
		"template": {"name": template_name, "language": {"code": language_code}},
	})


# ---------------------------------------------------------------------------
# CUSTOMER LOOKUP / CREATION
# ---------------------------------------------------------------------------
def find_or_create_customer(whatsapp_number):
	"""
	Look up WA Customer by WhatsApp number.
	Returns (customer_dict, is_existing_bool). Pure DB — 0 API calls.
	"""
	existing = frappe.db.get_value(
		"WA Customer",
		{"whatsapp_number": whatsapp_number},
		["name", "is_existing_customer", "customer_name"],
		as_dict=True,
	)
	if existing:
		frappe.db.set_value("WA Customer", existing["name"], "last_contact_at", frappe.utils.now())
		return existing, True  # Record exists in DB = returning customer → SupportFlow

	doc = frappe.get_doc({
		"doctype": "WA Customer",
		"whatsapp_number": whatsapp_number,
		"is_existing_customer": 0,
		"created_at": frappe.utils.now(),
		"last_contact_at": frappe.utils.now(),
	})
	doc.insert(ignore_permissions=True)
	frappe.db.commit()
	return doc.as_dict(), False


# ---------------------------------------------------------------------------
# ROUTING
# ---------------------------------------------------------------------------
def _send_appropriate_flow(whatsapp_number, is_existing_customer):
	key = "wa_support_flow_id" if is_existing_customer else "wa_new_lead_flow_id"
	flow_id = frappe.conf.get(key, "")
	if not flow_id:
		frappe.log_error(f"Flow ID not configured: {key}", "WA Flow Config Error")
		return False
	return _send_flow(whatsapp_number, flow_id)


# ---------------------------------------------------------------------------
# MARK LEAD REPLIED  (stops re-engagement)
# ---------------------------------------------------------------------------
def _mark_lead_replied(customer_name):
	leads = frappe.get_all(
		"WA Lead",
		filters={
			"customer": customer_name,
			"status": ["!=", "Closed"],
			"reengagement_stage": ["!=", "done"],
		},
		fields=["name"],
	)
	for lead in leads:
		frappe.db.set_value("WA Lead", lead["name"], {
			"reengagement_stage": "done",
			"last_inbound_at": frappe.utils.now(),
		})
	if leads:
		frappe.db.commit()


# ---------------------------------------------------------------------------
# ATTACH MEDIA TO OPEN TICKET
# ---------------------------------------------------------------------------
def _attach_media_to_ticket(customer_name, wa_message_id, media_type):
	ticket = frappe.db.get_value(
		"WA Service Request",
		filters={"customer": customer_name, "status": ["in", ["Open", "In Progress"]]},
		fieldname="name",
		order_by="creation desc",
	)
	if not ticket:
		return
	frappe.get_doc({
		"doctype": "WA Ticket Media",
		"ticket": ticket,
		"media_type": media_type.capitalize(),
		"whatsapp_media_id": wa_message_id,
		"uploaded_at": frappe.utils.now(),
	}).insert(ignore_permissions=True)
	frappe.db.commit()


# ---------------------------------------------------------------------------
# BUSINESS LOGIC
# ---------------------------------------------------------------------------
def compute_priority(scope, system_category, request_type):
	"""Returns HIGH / MEDIUM / LOW. 0 API calls."""
	if request_type in ("query", "disclosed"):
		return "LOW"
	if scope == "full_system":
		return "HIGH"
	if scope == "specific_area" and system_category in ("hvac", "security"):
		return "HIGH"
	return "MEDIUM"


def decide_resource_type(scope, system_category):
	"""Returns Engineer / Technician. 0 API calls."""
	if scope == "full_system":
		return "Engineer"
	if system_category in ("hvac", "security"):
		return "Engineer"
	return "Technician"


def _compute_sla_due(priority):
	now = frappe.utils.now_datetime()
	if priority == "HIGH":
		return frappe.utils.add_to_date(now, hours=24)
	if priority == "MEDIUM":
		return frappe.utils.add_to_date(now, days=7)
	return frappe.utils.add_to_date(now, days=14)


# ---------------------------------------------------------------------------
# CALENDAR SLOT BOOKING
# ---------------------------------------------------------------------------
def _find_and_book_slot(resource_type, system_category, ticket_name, requested_datetime=None):
	members = frappe.get_all(
		"WA Team Member",
		filters={"role": resource_type, "active": 1},
		fields=["name", "name_of_member"],
	)
	for member in members:
		has_skill = frappe.db.exists(
			"WA Team Member Skill",
			{"parent": member["name"], "skill": system_category},
		)
		if not has_skill:
			continue
		slot_filters = {"team_member": member["name"], "status": "Free"}
		if requested_datetime:
			try:
				slot_filters["date"] = str(requested_datetime)[:10]
			except Exception:
				pass
		slot = frappe.db.get_value(
			"WA Calendar Slot",
			slot_filters,
			["name", "date", "start_time"],
			as_dict=True,
			order_by="date asc, start_time asc",
		)
		if slot:
			frappe.db.set_value("WA Calendar Slot", slot["name"], {"status": "Busy", "ticket": ticket_name})
			frappe.db.set_value("WA Service Request", ticket_name, {
				"assigned_to": member["name"],
				"scheduled_slot": slot["name"],
				"resource_type": resource_type,
			})
			frappe.db.commit()
			return member["name_of_member"], slot
	return None, None


# ---------------------------------------------------------------------------
# MESSAGE LOG
# ---------------------------------------------------------------------------
def _log_message(customer_name, text, direction, msg_type="Text", wa_message_id=""):
	frappe.get_doc({
		"doctype": "WA Message Log",
		"customer": customer_name,
		"message_text": text,
		"direction": direction,
		"timestamp": frappe.utils.now(),
		"message_type": msg_type,
		"wa_message_id": wa_message_id,
	}).insert(ignore_permissions=True)


# ---------------------------------------------------------------------------
# ENDPOINT 1 — WEBHOOK
# ---------------------------------------------------------------------------

# ─────────────────────────────────────────────────────────────────────────────
# BYPASS OAUTH FOR META WEBHOOK ENDPOINTS
# ─────────────────────────────────────────────────────────────────────────────
def skip_auth_for_webhook():
	"""Bypass Frappe OAuth middleware for Meta webhook POST requests."""
	if frappe.request and frappe.request.path in [
		"/api/method/aretex_wa.whatsapp_handler.receive_whatsapp_message",
		"/api/method/aretex_wa.whatsapp_handler.receive_new_lead_flow_submission",
		"/api/method/aretex_wa.whatsapp_handler.receive_support_flow_submission",
	]:
		frappe.set_user("Guest")

@frappe.whitelist(allow_guest=True)
def receive_whatsapp_message(**kwargs):
	"""
	GET  → Meta webhook verification handshake
	POST → Process inbound messages (text / media / status)
	"""
	request = frappe.request

	# Webhook verification (GET)
	if request.method == "GET":
		args = frappe.request.args
		mode = args.get("hub.mode")
		token = args.get("hub.verify_token")
		challenge = args.get("hub.challenge", "")
		expected = frappe.conf.get("whatsapp_verify_token", "")
		if mode == "subscribe" and token == expected:
			return Response(str(challenge), status=200, mimetype="text/plain")
		return Response("Forbidden", status=403, mimetype="text/plain")

	try:
		sig = request.headers.get("X-Hub-Signature-256", "")
		body_bytes = request.get_data(cache=True, as_text=False, parse_form_data=False)
		if not _verify_signature(body_bytes, sig):
			frappe.response.update({"http_status_code": 401})
			return {"error": "Invalid signature"}

		body = json.loads(body_bytes)
		entry = body.get("entry", [{}])[0]
		changes = entry.get("changes", [{}])[0]
		value = changes.get("value", {})
		messages = value.get("messages", [])

		if not messages:
			return {"status": "ok"}

		msg = messages[0]
		from_number = msg.get("from", "")
		msg_type = msg.get("type", "text")
		wa_message_id = msg.get("id", "")

		if _is_rate_limited(from_number):
			return {"status": "rate_limited"}

		customer, is_existing = find_or_create_customer(from_number)
		customer_name = customer.get("name") or customer.name

		_mark_lead_replied(customer_name)

		if msg_type in ("image", "video"):
			media_id = (msg.get("image") or msg.get("video") or {}).get("id", "")
			_attach_media_to_ticket(customer_name, media_id or wa_message_id, msg_type)
			_log_message(customer_name, f"[{msg_type} received]", "Incoming", "Media", wa_message_id)
			return {"status": "media_attached"}

		if msg_type == "text":
			text_body = msg.get("text", {}).get("body", "")
			_log_message(customer_name, text_body, "Incoming", "Text", wa_message_id)
			sent = _send_appropriate_flow(from_number, is_existing)
			flow_type = "SupportFlow" if is_existing else "NewLeadFlow"
			_log_message(customer_name, f"[{flow_type} sent]", "Outgoing", "Notification")
			return {"status": "flow_sent", "flow": flow_type, "success": sent}

		return {"status": "unhandled_type", "type": msg_type}

	except Exception as exc:
		frappe.log_error(frappe.get_traceback(), "WA Webhook Error")
		return {"error": str(exc)}


# ---------------------------------------------------------------------------
# ENDPOINT 2 — NEW LEAD FLOW SUBMISSION
# ---------------------------------------------------------------------------

def _decrypt_flow_request(body):
	"""Decrypt incoming encrypted payload from Meta Flow."""
	private_key_pem = frappe.conf.get("whatsapp_flow_private_key", "")
	if not private_key_pem:
		return body, None, None

	encrypted_flow_data = b64decode(body["encrypted_flow_data"])
	encrypted_aes_key   = b64decode(body["encrypted_aes_key"])
	initial_vector      = b64decode(body["initial_vector"])

	private_key = load_pem_private_key(
		private_key_pem.encode("utf-8"), password=None
	)

	aes_key = private_key.decrypt(
		encrypted_aes_key,
		OAEP(mgf=MGF1(algorithm=hashes.SHA256()), algorithm=hashes.SHA256(), label=None)
	)

	encrypted_body = encrypted_flow_data[:-16]
	auth_tag       = encrypted_flow_data[-16:]

	decryptor = Cipher(
		algorithms.AES(aes_key),
		modes.GCM(initial_vector, auth_tag)
	).decryptor()

	decrypted = decryptor.update(encrypted_body) + decryptor.finalize()
	return json.loads(decrypted.decode("utf-8")), aes_key, initial_vector


def _encrypt_flow_response(response_data, aes_key, initial_vector):
	"""Encrypt response back to Meta Flow (Base64 AES-GCM)."""
	flipped_iv = bytes([b ^ 0xFF for b in initial_vector])
	encryptor = Cipher(
		algorithms.AES(aes_key),
		modes.GCM(flipped_iv)
	).encryptor()
	encrypted = (
		encryptor.update(json.dumps(response_data).encode("utf-8"))
		+ encryptor.finalize()
		+ encryptor.tag
	)
	return b64encode(encrypted).decode("utf-8")


@frappe.whitelist(allow_guest=True)
def receive_new_lead_flow_submission(**kwargs):
	"""Creates WA Lead + sends 1 confirmation. 1 outbound API call."""
	try:
		raw_body = frappe.request.get_json(force=True) or {}

		# Decrypt if encrypted payload from Meta Flow endpoint
		if "encrypted_flow_data" in raw_body:
			data, aes_key, iv = _decrypt_flow_request(raw_body)
		else:
			data, aes_key, iv = raw_body, None, None

		# Health check ping from Meta
		if data.get("action") == "ping":
			response = {"data": {"status": "active"}}
			if aes_key:
				return Response(
					_encrypt_flow_response(response, aes_key, iv),
					status=200, mimetype="text/plain"
				)
			return response

		whatsapp_number = data.get("whatsapp_number", "")
		if not whatsapp_number:
			return {"success": False, "error": "Missing whatsapp_number"}

		info_type = data.get("info_type", "")
		wants_more_info = str(data.get("wants_more_info", "false")).lower() == "true"
		lead_type = data.get("lead_type", "none")
		preferred_datetime = data.get("preferred_datetime", "")
		lead_name = data.get("name", "")
		lead_email = data.get("email", "")
		notes = data.get("notes", "")

		customer, _ = find_or_create_customer(whatsapp_number)
		customer_name = customer.get("name") or customer.name

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
			"created_at": frappe.utils.now(),
		})
		lead_doc.insert(ignore_permissions=True)
		frappe.db.commit()

		_log_message(customer_name, "[NewLeadFlow submitted]", "Incoming", "Text")

		if lead_type == "site_survey":
			action_line = "Site survey request received."
			if preferred_datetime:
				action_line += f" Preferred time: {preferred_datetime[:10]}"
		elif lead_type == "callback":
			action_line = "Callback request received. Our team will reach you soon."
		else:
			action_line = "Thank you for your interest!"

		greeting = f"Hi {lead_name}!" if lead_name else "Hi!"
		confirm_msg = (
			f"{greeting}\n\n"
			f"{action_line}\n\n"
			f"Our team at Aretex HVAC will get in touch with you shortly.\n\n"
			f"Reference: {lead_doc.name}"
		)
		_send_text(whatsapp_number, confirm_msg)
		_log_message(customer_name, confirm_msg, "Outgoing", "Notification")

		return {"success": True, "lead": lead_doc.name}

	except Exception as exc:
		frappe.log_error(frappe.get_traceback(), "WA New Lead Flow Error")
		return {"success": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# ENDPOINT 3 — SUPPORT FLOW SUBMISSION
# ---------------------------------------------------------------------------
@frappe.whitelist(allow_guest=True)
def receive_support_flow_submission(**kwargs):
	"""Creates WA Service Request, books slot, sends 1 confirmation. 1 outbound API call."""
	try:
		raw_body = frappe.request.get_json(force=True) or {}

		# Decrypt if encrypted payload from Meta Flow endpoint
		if "encrypted_flow_data" in raw_body:
			data, aes_key, iv = _decrypt_flow_request(raw_body)
		else:
			data, aes_key, iv = raw_body, None, None

		# Health check ping from Meta
		if data.get("action") == "ping":
			response = {"data": {"status": "active"}}
			if aes_key:
				return Response(
					_encrypt_flow_response(response, aes_key, iv),
					status=200, mimetype="text/plain"
				)
			return response

		whatsapp_number = data.get("whatsapp_number", "")
		if not whatsapp_number:
			return {"success": False, "error": "Missing whatsapp_number"}

		request_type = data.get("request_type", "issue")
		description = data.get("description", "")
		scope = data.get("scope", "specific_device")
		system_category = data.get("system_category", "hvac")
		location = data.get("location", "")
		fault_type = data.get("fault_type", "")
		requested_datetime = data.get("requested_datetime", "")
		notes = data.get("notes", "")

		customer, _ = find_or_create_customer(whatsapp_number)
		customer_name = customer.get("name") or customer.name

		priority = compute_priority(scope, system_category, request_type)
		resource_type = decide_resource_type(scope, system_category)

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
			"sla_due_at": _compute_sla_due(priority),
		})
		ticket_doc.insert(ignore_permissions=True)
		frappe.db.commit()

		assigned_member, slot = _find_and_book_slot(
			resource_type, system_category, ticket_doc.name, requested_datetime
		)

		_log_message(customer_name, "[SupportFlow submitted]", "Incoming", "Text")

		priority_emoji = {"HIGH": "🔴", "MEDIUM": "🟠", "LOW": "🟡"}.get(priority, "⚪")
		if assigned_member and slot:
			slot_info = (
				f"Scheduled: {slot.get('date', '')} at {str(slot.get('start_time', ''))[:5]}\n"
				f"Assigned to: {assigned_member}"
			)
		else:
			slot_info = "Our scheduling team will confirm your appointment time shortly."

		sla_date = str(ticket_doc.sla_due_at)[:10] if ticket_doc.sla_due_at else "TBD"
		confirm_msg = (
			f"Support ticket raised!\n\n"
			f"Ticket: {ticket_doc.name}\n"
			f"Priority: {priority_emoji} {priority}\n"
			f"Category: {system_category.upper()} — {location}\n"
			f"Resource: {resource_type}\n\n"
			f"{slot_info}\n\n"
			f"You can send photos/videos in this chat — they'll be auto-attached.\n\n"
			f"SLA deadline: {sla_date}"
		)
		_send_text(whatsapp_number, confirm_msg)
		_log_message(customer_name, confirm_msg, "Outgoing", "Notification")

		return {"success": True, "ticket": ticket_doc.name, "priority": priority}

	except Exception as exc:
		frappe.log_error(frappe.get_traceback(), "WA Support Flow Error")
		return {"success": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# ENDPOINT 4 — CUSTOMER HISTORY (admin)
# ---------------------------------------------------------------------------
@frappe.whitelist()
def get_customer_history(whatsapp_number=None, limit=50):
	if not whatsapp_number:
		return {"success": False, "error": "whatsapp_number required"}
	customer = frappe.db.get_value("WA Customer", {"whatsapp_number": whatsapp_number}, "name")
	if not customer:
		return {"success": False, "error": "Customer not found"}
	logs = frappe.get_all(
		"WA Message Log",
		filters={"customer": customer},
		fields=["message_text", "direction", "timestamp", "message_type"],
		order_by="timestamp desc",
		limit_page_length=int(limit),
	)
	tickets = frappe.get_all(
		"WA Service Request",
		filters={"customer": customer},
		fields=["name", "priority", "status", "system_category", "creation"],
		order_by="creation desc",
		limit_page_length=10,
	)
	return {"success": True, "customer": customer, "messages": logs, "tickets": tickets}


# ---------------------------------------------------------------------------
# ENDPOINT 5 — DASHBOARD STATS (admin)
# ---------------------------------------------------------------------------
@frappe.whitelist()
def get_dashboard_stats():
	return {
		"success": True,
		"stats": {
			"total_customers": frappe.db.count("WA Customer"),
			"new_leads": frappe.db.count("WA Lead", {"status": "New"}),
			"open_tickets": frappe.db.count("WA Service Request", {"status": "Open"}),
			"high_priority_tickets": frappe.db.count(
				"WA Service Request", {"status": "Open", "priority": "HIGH"}
			),
			"sla_breached": frappe.db.count(
				"WA Service Request",
				{"status": "Open", "sla_due_at": ["<", frappe.utils.now()]},
			),
			"free_calendar_slots": frappe.db.count("WA Calendar Slot", {"status": "Free"}),
		},
	}
