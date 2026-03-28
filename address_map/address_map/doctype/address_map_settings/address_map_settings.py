# Copyright (c) 2026, ALITECS Alexander Leisentritt and contributors
# License: GPLv3

import frappe
from frappe.model.document import Document


class AddressMapSettings(Document):
	def validate(self):
		seen: set[str] = set()
		for row in self.get("doctypes") or []:
			name = str(row.display_name or "").strip()
			if not name:
				continue
			if name in seen:
				frappe.throw(frappe._("Duplicate view name \"{0}\" in Views list. Each view must have a unique Name.").format(name))
			seen.add(name)
