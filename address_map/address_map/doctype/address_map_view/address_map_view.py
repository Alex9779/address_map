import frappe
from frappe.model.document import Document


class AddressMapView(Document):
	def validate(self):
		self.display_name = str(self.display_name or "").strip()
		if not self.display_name:
			frappe.throw(frappe._("Name is required."))

		existing = frappe.db.get_value(
			"Address Map View",
			{"display_name": self.display_name, "name": ["!=", self.name or ""]},
			"name",
		)
		if existing:
			frappe.throw(
				frappe._("Duplicate view name \"{0}\". Each view must have a unique Name.").format(self.display_name)
			)
