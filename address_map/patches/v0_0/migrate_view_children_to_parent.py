import frappe


def _normalize(value: str | None) -> str:
	return str(value or "").strip()


def execute() -> None:
	"""Move view sub-config rows under Address Map View docs.

	Old model:
	- Address Map View rows are children of Address Map Settings.doctypes
	- Address Map Rule rows are children of Address Map Settings.rules keyed by display_name
	- Address Map Popup Field rows are children of Address Map Settings.popup_fields keyed by view_name

	New model:
	- Address Map View is a standalone doctype
	- Address Map Rule rows are children of Address Map View.rules
	- Address Map Popup Field rows are children of Address Map View.popup_fields
	"""
	if not frappe.db.table_exists("tabAddress Map View"):
		return

	# 1) Detach existing view rows from Address Map Settings, preserving their names.
	legacy_views = frappe.db.get_all(
		"Address Map View",
		filters={"parent": "Address Map Settings", "parenttype": "Address Map Settings", "parentfield": "doctypes"},
		fields=["name", "display_name"],
		order_by="idx",
	)
	for idx, row in enumerate(legacy_views, start=1):
		frappe.db.sql(
			"""
			UPDATE `tabAddress Map View`
			SET parent = NULL,
				parenttype = NULL,
				parentfield = NULL,
				idx = %s
			WHERE name = %s
			""",
			(idx, row.name),
		)

	# Build lookup for assigning subtype rows.
	all_views = frappe.db.get_all("Address Map View", fields=["name", "display_name"])
	view_by_display_name: dict[str, str] = {}
	for row in all_views:
		display_name = _normalize(row.display_name)
		if display_name and display_name not in view_by_display_name:
			view_by_display_name[display_name] = str(row.name)

	# 2) Re-parent legacy rules to the matching Address Map View document.
	legacy_rules = frappe.db.get_all(
		"Address Map Rule",
		filters={"parent": "Address Map Settings", "parenttype": "Address Map Settings", "parentfield": "rules"},
		fields=["name", "display_name"],
		order_by="idx",
	)
	rule_idx_by_view: dict[str, int] = {}
	for row in legacy_rules:
		view_name = view_by_display_name.get(_normalize(row.display_name))
		if not view_name:
			continue
		rule_idx_by_view[view_name] = rule_idx_by_view.get(view_name, 0) + 1
		frappe.db.sql(
			"""
			UPDATE `tabAddress Map Rule`
			SET parent = %s,
				parenttype = 'Address Map View',
				parentfield = 'rules',
				idx = %s
			WHERE name = %s
			""",
			(view_name, rule_idx_by_view[view_name], row.name),
		)

	# 3) Re-parent legacy popup field rows to the matching Address Map View document.
	legacy_popup_fields = frappe.db.get_all(
		"Address Map Popup Field",
		filters={"parent": "Address Map Settings", "parenttype": "Address Map Settings", "parentfield": "popup_fields"},
		fields=["name", "view_name"],
		order_by="idx",
	)
	popup_idx_by_view: dict[str, int] = {}
	for row in legacy_popup_fields:
		view_name = view_by_display_name.get(_normalize(row.view_name))
		if not view_name:
			continue
		popup_idx_by_view[view_name] = popup_idx_by_view.get(view_name, 0) + 1
		frappe.db.sql(
			"""
			UPDATE `tabAddress Map Popup Field`
			SET parent = %s,
				parenttype = 'Address Map View',
				parentfield = 'popup_fields',
				idx = %s
			WHERE name = %s
			""",
			(view_name, popup_idx_by_view[view_name], row.name),
		)

	frappe.clear_cache(doctype="Address Map View")
	frappe.clear_cache(doctype="Address Map Settings")
