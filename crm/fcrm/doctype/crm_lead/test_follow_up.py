from unittest.mock import patch

import frappe
from frappe.utils import add_to_date, now_datetime
from frappe.utils.jinja import get_email_from_template

from crm.fcrm.doctype.crm_lead import follow_up
from crm.fcrm.doctype.crm_lead.crm_lead import mark_converted
from crm.tests import CRMTestCase

FALLBACK_EMAIL = "fallback-sales@example.com"


def make_lead(minutes_ahead: int, follow_up_type: str = follow_up.PHONE_CALL, **values):
	return frappe.get_doc(
		{
			"doctype": "CRM Lead",
			"first_name": "Amit",
			"organization": "Bharat Chemist",
			"area": "Kandivali West",
			"pincode": "400067",
			"mobile_no": "98765 43210",
			"lead_owner": "Administrator",
			"follow_up_type": follow_up_type,
			"follow_up_date_time": add_to_date(now_datetime(), minutes=minutes_ahead),
			**values,
		}
	).insert()


def send_with_mock_mail(lead):
	with patch("frappe.sendmail") as sendmail:
		follow_up.send_reminder(lead.name)
	return sendmail


def render_email(lead) -> str:
	return get_email_from_template("crm_follow_up_reminder", follow_up.get_email_args(lead))[0]


class TestFollowUp(CRMTestCase):
	def setUp(self):
		frappe.db.set_single_value(
			"FCRM Settings",
			{
				"call_reminder_advance_minutes": "15",
				"visit_reminder_advance_minutes": "30",
				"fallback_sales_notification_email": FALLBACK_EMAIL,
			},
		)
		frappe.clear_document_cache("FCRM Settings")

	def tearDown(self):
		frappe.db.rollback()
		frappe.clear_document_cache("FCRM Settings")

	def test_unset_advance_falls_back_to_field_default(self):
		frappe.db.set_single_value("FCRM Settings", "visit_reminder_advance_minutes", None)
		frappe.clear_document_cache("FCRM Settings")

		self.assertEqual(follow_up.advance_minutes(follow_up.PHARMACY_VISIT), 30)

	def test_rejects_follow_up_less_than_five_minutes_ahead(self):
		with self.assertRaises(frappe.ValidationError):
			make_lead(minutes_ahead=2)

	def test_sends_once_to_owner_when_due(self):
		lead = make_lead(minutes_ahead=10)

		with patch("frappe.sendmail") as sendmail:
			follow_up.send_reminder(lead.name)
			follow_up.send_reminder(lead.name)

		sendmail.assert_called_once()
		self.assertEqual(sendmail.call_args.kwargs["recipients"], ["admin@example.com"])
		self.assertTrue(frappe.db.get_value("CRM Lead", lead.name, "follow_up_notification_sent"))

	def test_skips_lead_converted_after_scheduling(self):
		lead = make_lead(minutes_ahead=10)
		mark_converted(lead)  # writes with db_set, so no lead hooks run

		send_with_mock_mail(lead).assert_not_called()

	def test_status_change_cancels_pending_reminder(self):
		for status in ("Contacted", "Qualified"):
			with self.subTest(status=status):
				lead = make_lead(minutes_ahead=10)

				lead.status = status
				lead.save()

				self.assertTrue(lead.follow_up_notification_sent)
				send_with_mock_mail(lead).assert_not_called()

	def test_new_follow_up_after_contacted_is_reminded(self):
		lead = make_lead(minutes_ahead=10)
		lead.status = "Contacted"
		lead.save()

		lead.follow_up_date_time = add_to_date(now_datetime(), minutes=12)
		lead.save()

		send_with_mock_mail(lead).assert_called_once()

	def test_new_time_saved_with_contacted_is_reminded(self):
		lead = make_lead(minutes_ahead=10)

		lead.status = "Contacted"
		lead.follow_up_date_time = add_to_date(now_datetime(), minutes=12)
		lead.save()

		send_with_mock_mail(lead).assert_called_once()

	def test_reschedule_rearms_and_defers(self):
		lead = make_lead(minutes_ahead=10)
		lead.db_set("follow_up_notification_sent", 1)
		lead.reload()

		lead.follow_up_date_time = add_to_date(now_datetime(), minutes=120)
		lead.save()

		self.assertFalse(lead.follow_up_notification_sent)
		send_with_mock_mail(lead).assert_not_called()

	def test_unassigned_lead_goes_to_fallback_inbox(self):
		lead = make_lead(minutes_ahead=20, follow_up_type=follow_up.PHARMACY_VISIT, lead_owner=None)

		sendmail = send_with_mock_mail(lead)

		self.assertEqual(sendmail.call_args.kwargs["recipients"], [FALLBACK_EMAIL])
		self.assertTrue(sendmail.call_args.kwargs["subject"].startswith("[UNASSIGNED - VISIT at "))

	def test_scheduler_enqueues_only_due_leads(self):
		due_call = make_lead(minutes_ahead=10)
		later_call = make_lead(minutes_ahead=25)
		due_visit = make_lead(minutes_ahead=25, follow_up_type=follow_up.PHARMACY_VISIT)

		with patch.object(follow_up, "enqueue_reminder") as enqueue:
			follow_up.send_due_reminders()

		queued = {call.args[0] for call in enqueue.call_args_list}
		self.assertTrue({due_call.name, due_visit.name} <= queued)
		self.assertNotIn(later_call.name, queued)

	def test_scheduler_skips_converted_leads(self):
		lead = make_lead(minutes_ahead=10)
		mark_converted(lead)

		with patch.object(follow_up, "enqueue_reminder") as enqueue:
			follow_up.send_due_reminders()

		self.assertNotIn(lead.name, {call.args[0] for call in enqueue.call_args_list})

	def test_subjects_follow_ticket_format(self):
		at = "2026-11-10 15:00:00"
		visit = frappe._dict(
			follow_up_type=follow_up.PHARMACY_VISIT,
			follow_up_date_time=at,
			organization="Bharat Chemist",
			lead_name="Amit",
			area="Kandivali West",
		)
		call = frappe._dict(
			follow_up_type=follow_up.PHONE_CALL, follow_up_date_time=at, lead_name="Dr. Alok Verma"
		)

		self.assertEqual(
			follow_up.get_subject(visit, unassigned=False),
			"[VISIT at 03:00 PM] Bharat Chemist - Kandivali West",
		)
		self.assertEqual(follow_up.get_subject(call, unassigned=False), "[CALL at 03:00 PM] Dr. Alok Verma")
		self.assertEqual(
			follow_up.get_subject(visit, unassigned=True), "[UNASSIGNED - VISIT at 03:00 PM] Bharat Chemist"
		)

	def test_links(self):
		self.assertEqual(follow_up.tel_link("98765 43210"), "tel:+919876543210")
		self.assertEqual(follow_up.tel_link("+91-98765-43210"), "tel:+919876543210")
		self.assertEqual(follow_up.tel_link("022 2345 6789"), "tel:+912223456789")
		self.assertIsNone(follow_up.maps_link(None, ""))
		self.assertIn(
			"destination=Bharat+Chemist%2C+400067", follow_up.maps_link("Bharat Chemist", None, "400067")
		)

	def test_email_renders_call_and_map_links(self):
		lead = make_lead(minutes_ahead=20, follow_up_type=follow_up.PHARMACY_VISIT, address_line="Shop 4")

		html = render_email(lead)

		self.assertIn('href="tel:+919876543210"', html)
		self.assertIn("https://www.google.com/maps/dir/?api=1", html)
		self.assertIn(f"/crm/leads/{lead.name}", html)

	def test_visit_email_follows_ticket_layout(self):
		lead = make_lead(
			minutes_ahead=20,
			follow_up_type=follow_up.PHARMACY_VISIT,
			address_line="Shop 4",
			follow_up_notes="Bring the demo tablet",
		)

		html = render_email(lead)

		ticket_labels = (
			"AUSHADHI CRM | FOLLOW-UP ALERT",
			"UPCOMING PHARMACY VISIT IN",
			"Target Time:",
			"STORE DETAILS",
			"Pharmacy:",
			"Contact:",
			"Mobile:",
			"TAP TO CALL",
			"STORE LOCATION",
			"OPEN IN GOOGLE MAPS",
			"PREVIOUS NOTES / CONTEXT",
			"OPEN LEAD IN CRM",
		)
		positions = [html.index(label) for label in ticket_labels]
		self.assertEqual(positions, sorted(positions))

	def test_call_email_has_no_store_location(self):
		lead = make_lead(minutes_ahead=10)

		html = render_email(lead)

		self.assertIn("UPCOMING CALL IN", html)
		self.assertIn("CONTACT DETAILS", html)
		self.assertNotIn("STORE LOCATION", html)
		self.assertNotIn("Pharmacy:", html)
