"""Show the follow-up and address fields in the lead side panel on existing sites.

``crm/install.py`` seeds layouts only when they are missing, so sites installed before
these fields existed need the sections appended.
"""

import json

import frappe

LAYOUT = "CRM Lead-Side Panel"
SECTIONS = [
	{
		"label": "Follow-up",
		"name": "follow_up_section",
		"opened": True,
		"columns": [
			{"name": "column_Fu7q", "fields": ["follow_up_type", "follow_up_date_time", "follow_up_notes"]}
		],
	},
	{
		"label": "Address",
		"name": "address_section",
		"opened": True,
		"columns": [{"name": "column_Ad3x", "fields": ["address_line", "area", "pincode"]}],
	},
]


def execute():
	if not frappe.db.exists("CRM Fields Layout", LAYOUT):
		return

	doc = frappe.get_doc("CRM Fields Layout", LAYOUT)
	layout = json.loads(doc.layout or "[]")
	present = {section.get("name") for section in layout}
	missing = [section for section in SECTIONS if section["name"] not in present]
	if missing:
		doc.layout = json.dumps(layout + missing)
		doc.save()
