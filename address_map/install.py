# Copyright (c) 2026, ALITECS Alexander Leisentritt and contributors
# License: GPLv3

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

from .custom_fields import get_custom_fields


def after_install():
	create_custom_fields(get_custom_fields())


def before_uninstall():
	for doctype, fields in get_custom_fields().items():
		for fieldname in [field["fieldname"] for field in fields]:
			if frappe.db.exists("Custom Field", {"dt": doctype, "fieldname": fieldname}):
				frappe.delete_doc("Custom Field", f"{doctype}-{fieldname}", ignore_missing=True)
