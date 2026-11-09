"""Email the lead owner shortly before a scheduled phone call or pharmacy visit."""

import re
from datetime import datetime, timedelta
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import frappe
from frappe import _
from frappe.utils import add_to_date, get_datetime, get_system_timezone, get_url, now_datetime

PHONE_CALL = "Phone Call"
PHARMACY_VISIT = "Pharmacy Visit"
CONTACTED = "Contacted"
ADVANCE_SETTING = {
	PHONE_CALL: "call_reminder_advance_minutes",
	PHARMACY_VISIT: "visit_reminder_advance_minutes",
}
MIN_NOTICE_MINUTES = 5
CLOSED_STATUS_TYPES = ("Won", "Lost")
DEFAULT_COUNTRY_CODE = "+91"


def validate(lead, method=None) -> None:
	if not (lead.has_value_changed("follow_up_date_time") or lead.has_value_changed("follow_up_type")):
		# Only an untouched follow-up is cancelled; one saved with the status change stays armed.
		if lead.has_value_changed("status") and cancels_reminder(lead):
			lead.follow_up_notification_sent = 1
		return

	lead.follow_up_notification_sent = 0
	if not lead.follow_up_date_time:
		return
	if not lead.follow_up_type:
		frappe.throw(_("Follow-up type is required when a follow-up time is set."))
	if get_datetime(lead.follow_up_date_time) < add_to_date(now_datetime(), minutes=MIN_NOTICE_MINUTES):
		frappe.throw(_("Scheduled follow-up time must be in the future."))


def on_update(lead, method=None) -> None:
	# A follow-up saved inside its reminder window should not wait for the next scheduler tick.
	if lead.follow_up_date_time and not lead.follow_up_notification_sent and is_due(lead, now_datetime()):
		enqueue_reminder(lead.name)


def send_due_reminders() -> None:
	now = now_datetime()
	horizon = add_to_date(now, minutes=max(map(advance_minutes, ADVANCE_SETTING)))
	leads = frappe.get_all(
		"CRM Lead",
		filters=[
			["follow_up_notification_sent", "=", 0],
			["converted", "=", 0],
			["follow_up_date_time", ">", now],
			["follow_up_date_time", "<=", horizon],
		],
		fields=["name", "follow_up_type", "follow_up_date_time"],
	)
	for lead in leads:
		if is_due(lead, now):
			enqueue_reminder(lead.name)


def enqueue_reminder(lead_name: str) -> None:
	frappe.enqueue(
		send_reminder,
		lead_name=lead_name,
		job_id=f"crm_follow_up_reminder::{lead_name}",
		deduplicate=True,
		enqueue_after_commit=True,
	)


def send_reminder(lead_name: str) -> None:
	# The row lock makes the scheduler and the on-save path agree on who sends.
	lead = frappe.get_doc("CRM Lead", lead_name, for_update=True)
	if lead.follow_up_notification_sent or is_closed(lead) or not is_due(lead, now_datetime()):
		return

	lead.db_set("follow_up_notification_sent", 1, update_modified=False)
	recipient, unassigned = get_recipient(lead)
	if not recipient:
		frappe.log_error(
			title=_("Follow-up reminder has no recipient"),
			message=_("Set a fallback notification email in FCRM Settings."),
			reference_doctype="CRM Lead",
			reference_name=lead.name,
		)
		return

	frappe.sendmail(
		recipients=[recipient],
		subject=get_subject(lead, unassigned),
		template="crm_follow_up_reminder",
		args=get_email_args(lead),
		reference_doctype="CRM Lead",
		reference_name=lead.name,
		add_unsubscribe_link=0,
		now=True,
	)


def advance_minutes(follow_up_type: str) -> int:
	# Settings saved before these fields existed hold no value, so fall back to the field default.
	field = ADVANCE_SETTING[follow_up_type]
	value = frappe.get_cached_doc("FCRM Settings").get(field)
	return int(value or frappe.get_meta("FCRM Settings").get_field(field).default)


def is_due(lead, now: datetime) -> bool:
	follow_up_at = get_datetime(lead.follow_up_date_time)
	return follow_up_at - timedelta(minutes=advance_minutes(lead.follow_up_type)) <= now < follow_up_at


def is_closed(lead) -> bool:
	return bool(lead.converted) or (
		frappe.get_cached_value("CRM Lead Status", lead.status, "type") in CLOSED_STATUS_TYPES
	)


def cancels_reminder(lead) -> bool:
	return lead.status == CONTACTED or is_closed(lead)


def get_recipient(lead) -> tuple[str | None, bool]:
	"""The owner's email, or the fallback inbox when the lead has no active owner."""
	email = lead.lead_owner and frappe.db.get_value("User", {"name": lead.lead_owner, "enabled": 1}, "email")
	if email:
		return email, False
	return frappe.get_cached_doc("FCRM Settings").fallback_sales_notification_email, True


def get_subject(lead, unassigned: bool) -> str:
	kind = "VISIT" if lead.follow_up_type == PHARMACY_VISIT else "CALL"
	slot = f"{kind} at {get_datetime(lead.follow_up_date_time):%I:%M %p}"
	title = lead.organization or lead.lead_name

	if unassigned:
		return f"[UNASSIGNED - {slot}] {title}"
	if kind == "VISIT":
		return f"[{slot}] {title} - {lead.area}" if lead.area else f"[{slot}] {title}"
	if lead.organization and lead.lead_name != lead.organization:
		return f"[{slot}] {lead.organization} ({lead.lead_name})"
	return f"[{slot}] {title}"


def get_email_args(lead) -> dict:
	follow_up_at = get_datetime(lead.follow_up_date_time)
	phone = lead.mobile_no or lead.phone
	return {
		"lead": lead,
		"is_visit": lead.follow_up_type == PHARMACY_VISIT,
		"minutes_left": round((follow_up_at - now_datetime()).total_seconds() / 60),
		"when": follow_up_at.replace(tzinfo=ZoneInfo(get_system_timezone())).strftime("%d-%b-%Y %I:%M %p %Z"),
		"phone": phone,
		"tel_link": tel_link(phone) if phone else None,
		"maps_link": maps_link(lead.organization, lead.address_line, lead.area, lead.pincode),
		"lead_url": get_url(f"/crm/leads/{lead.name}"),
	}


def tel_link(number: str) -> str:
	"""Numbers saved without a country code are taken as Indian."""
	digits = re.sub(r"[^\d+]", "", number)
	return "tel:" + (digits if digits.startswith("+") else DEFAULT_COUNTRY_CODE + digits[-10:])


def maps_link(*parts: str | None) -> str | None:
	destination = ", ".join(part for part in parts if part)
	if not destination:
		return None
	query = urlencode({"api": 1, "destination": destination, "dir_action": "navigate"})
	return f"https://www.google.com/maps/dir/?{query}"
