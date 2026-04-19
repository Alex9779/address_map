# Copyright (c) 2026, ALITECS Alexander Leisentritt and contributors
# License: GPLv3

import json
import re
import urllib.parse
from typing import cast

import frappe
from frappe.utils.data import escape_html

_dict = frappe._dict


@frappe.whitelist()
def get_views() -> list[dict]:
	"""Return the views configured in Address Map Settings.

	Each row may optionally specify a via_doctype to route through a Level-1
	DocType (e.g. Sales Invoice → Customer → Address).

	Each entry: {doctype, via, via_field, label}
	"""
	# frappe.db.get_all silently strips child-table fields that are not in the
	# default field set when no parent_doctype context is available at the ORM
	# layer.  Reading the parent document directly avoids this problem.
	settings = frappe.get_cached_doc("Address Map Settings")
	rows = cast(list[_dict], settings.get("doctypes") or [])

	result = []
	for row in rows:
		doctype = str(row.doctype_name or "").strip()
		via = str(row.via_doctype or "").strip() or None
		if not doctype:
			continue
		if not frappe.has_permission(doctype, "read"):
			continue
		via_field = None
		if via:
			if not frappe.has_permission(via, "read"):
				continue
			via_field = _find_via_field(doctype, via)
			if not via_field:
				continue  # skip invalid / unresolvable paths
		label = str(row.display_name or "").strip() or frappe._(doctype)
		priority_raw = str(row.address_type_priority or "").strip()
		address_type_priority = [t.strip() for t in priority_raw.split(",") if t.strip()] if priority_raw else []
		result.append({"doctype": doctype, "via": via, "via_field": via_field, "label": label, "display_name": str(row.display_name or "").strip(), "allow_assign": bool(row.allow_assign), "address_type_priority": address_type_priority})
	return result


@frappe.whitelist()
def get_map_data(
	doctype: str,
	via: str | None = None,
	via_field: str | None = None,
	filters: str | list | None = None,
	display_name: str | None = None,
) -> dict:
	"""Return a GeoJSON FeatureCollection of geocoded addresses reachable from `doctype`.

	Level 1 (via=None):
	  Address.links → link_doctype=doctype

	Level 2 (via set):
	  {doctype}.{via_field} → {via}.name → Address.links

	filters: optional Frappe-style filters to restrict which docs are included.
	"""
	if via:
		_validate_level2_path(doctype, via, via_field)
	elif not frappe.has_permission("Address", "read"):
		frappe.throw(frappe._("Not Permitted"), frappe.PermissionError)

	if not frappe.has_permission(doctype, "read"):
		frappe.throw(frappe._("Not Permitted"), frappe.PermissionError)

	# Resolve filters to a set of allowed doc names
	allowed_names: set[str] | None = None
	if filters:
		if isinstance(filters, str):
			filters = cast(list, json.loads(filters))
		# FilterGroup.get_value() returns [doctype, fieldname, condition, value, hidden];
		# normalize to 3-element [fieldname, condition, value]
		normalized: list = []
		for f in filters:  # type: ignore[union-attr]
			if len(f) >= 4:
				normalized.append([f[1], f[2], f[3]])
			elif len(f) == 3:
				normalized.append(list(f))
		if normalized:
			names = cast(
				list[str],
				frappe.db.get_all(doctype, filters=normalized, pluck="name"),  # type: ignore[arg-type]
			)
			allowed_names = set(names)

	popup_fields = _get_popup_fields(display_name, doctype)
	line_fields = _get_popup_line_fields(display_name, doctype)
	rules = _get_rules(display_name, doctype)
	address_type_priority = _get_address_type_priority(display_name)

	if via:
		assert via_field is not None  # guaranteed by _validate_level2_path
		features = _map_data_level2(doctype, via, via_field, allowed_names, popup_fields, line_fields, rules, address_type_priority)
	else:
		features = _map_data_level1(doctype, allowed_names, popup_fields, line_fields, rules, address_type_priority)

	legend = [
		{
			"label": str(r.legend_label).strip(),
			"color": str(r.color or "").strip(),
			"shape": (str(r.shape or "") or "circle").lower(),
			"hide": bool(r.hide_marker),
		}
		for r in (rules or [])
		if str(r.get("legend_label") or "").strip()
	]

	return {"type": "FeatureCollection", "features": features, "legend": legend}


# ---------------------------------------------------------------------------
# Internal helpers — level-1
# ---------------------------------------------------------------------------

def _find_via_field(doctype: str, via: str) -> str | None:
	"""Return the first Link fieldname on `doctype` that points to `via`."""
	meta = frappe.get_meta(doctype)
	for field in meta.fields:
		if field.fieldtype == "Link" and field.options == via:
			return field.fieldname
	return None


def _map_data_level1(doctype: str, allowed_names: set[str] | None = None, popup_fields: list[dict] | None = None, line_fields: dict | None = None, rules: list[_dict] | None = None, address_type_priority: list[str] | None = None) -> list[dict]:
	params: dict = {"doctype": doctype}
	name_filter = ""
	if allowed_names is not None:
		if not allowed_names:
			return []
		params["allowed"] = list(allowed_names)
		name_filter = "AND dl.link_name IN %(allowed)s"

	rows = cast(list[_dict], frappe.db.sql(
		f"""
		SELECT
			dl.link_name,
			dl.link_title,
			addr.name       AS address_name,
			addr.address_title,
			addr.address_line1,
			addr.address_line2,
			addr.city,
			addr.state,
			addr.country,
			addr.pincode,
			addr.latitude,
			addr.longitude,
			addr.address_type AS address_type,
			doc_tbl.owner   AS owner,
			doc_tbl._assign AS _assign
		FROM `tabAddress` addr
		JOIN `tabDynamic Link` dl
			ON  dl.parent      = addr.name
			AND dl.parenttype  = 'Address'
			AND dl.link_doctype = %(doctype)s
			{name_filter}
		LEFT JOIN `tab{doctype}` doc_tbl ON doc_tbl.name = dl.link_name
		WHERE addr.latitude  IS NOT NULL
		  AND addr.longitude IS NOT NULL
		  AND addr.latitude  != 0
		  AND addr.longitude != 0
		""",
		params,
		as_dict=True,
	))
	rows = _pick_best_address(rows, address_type_priority)
	if popup_fields or line_fields:
		_enrich_rows(rows, doctype, popup_fields, line_fields)
	marker_map = _build_marker_map(doctype, [str(r.link_name) for r in rows], rules or [])
	rows = [r for r in rows if not (marker_map.get(str(r.link_name)) or {}).get("hide")]
	return [_to_feature(r, doctype, popup_fields, line_fields, marker_map) for r in rows]


# ---------------------------------------------------------------------------
# Internal helpers — level-2
# ---------------------------------------------------------------------------

def _validate_level2_path(doctype: str, via: str, via_field: str | None):
	"""Ensure the doctype→via→Address path is legitimate (not injected)."""
	if not via_field:
		frappe.throw(frappe._("Invalid map path: via_field is required for level-2 traversal"))

	# doctype+via must be a configured combination
	configured = frappe.db.get_all(
		"Address Map View",
		filters={"parent": "Address Map Settings", "parentfield": "doctypes", "doctype_name": doctype, "via_doctype": via},
		pluck="name",
	)
	if not configured:
		frappe.throw(frappe._("Invalid map path: {0} via {1} is not configured").format(doctype, via))

	# via_field must actually exist on doctype and point to via
	meta = frappe.get_meta(doctype)
	field = meta.get_field(via_field)
	if not field or field.fieldtype != "Link" or field.options != via:
		frappe.throw(
			frappe._("Invalid map path: {0}.{1} does not link to {2}").format(doctype, via_field, via)
		)


def _map_data_level2(doctype: str, via: str, via_field: str, allowed_names: set[str] | None = None, popup_fields: list[dict] | None = None, line_fields: dict | None = None, rules: list[_dict] | None = None, address_type_priority: list[str] | None = None) -> list[dict]:
	# via_field is validated by _validate_level2_path before this is called.
	# Table names and the via_field column name are built from validated metadata —
	# not from raw user input — so f-string interpolation is safe here.
	params: dict = {"via": via}
	name_filter = ""
	if allowed_names is not None:
		if not allowed_names:
			return []
		params["allowed"] = list(allowed_names)
		name_filter = "AND doc.name IN %(allowed)s"

	rows = cast(list[_dict], frappe.db.sql(
		f"""
		SELECT
			doc.name        AS link_name,
			doc.name        AS link_title,
			addr.name       AS address_name,
			addr.address_title,
			addr.address_line1,
			addr.address_line2,
			addr.city,
			addr.state,
			addr.country,
			addr.pincode,
			addr.latitude,
			addr.longitude,
			addr.address_type AS address_type,
			doc.owner       AS owner,
			doc._assign     AS _assign
		FROM `tab{doctype}` doc
		JOIN `tabDynamic Link` dl
			ON  dl.parenttype   = 'Address'
			AND dl.link_doctype = %(via)s
			AND dl.link_name    = doc.`{via_field}`
			{name_filter}
		JOIN `tabAddress` addr ON addr.name = dl.parent
		WHERE addr.latitude  IS NOT NULL
		  AND addr.longitude IS NOT NULL
		  AND addr.latitude  != 0
		  AND addr.longitude != 0
		""",
		params,
		as_dict=True,
	))
	rows = _pick_best_address(rows, address_type_priority)
	if popup_fields or line_fields:
		_enrich_rows(rows, doctype, popup_fields, line_fields)
	marker_map = _build_marker_map(doctype, [str(r.link_name) for r in rows], rules or [])
	rows = [r for r in rows if not (marker_map.get(str(r.link_name)) or {}).get("hide")]
	return [_to_feature(r, doctype, popup_fields, line_fields, marker_map) for r in rows]


# ---------------------------------------------------------------------------
# GeoJSON helpers
# ---------------------------------------------------------------------------

_POPUP_SKIP_TYPES: frozenset[str] = frozenset({
	"Section Break", "Column Break", "Tab Break", "HTML", "Button",
	"Image", "Signature", "Password", "Attach", "Attach Image",
	# Table fieldtypes have no DB column; bare table fields are skipped.
	# Use dot-notation (e.g. "service_technicians.technician") to show child rows.
	"Table", "Table MultiSelect",
})


def _get_rules(display_name: str | None, doctype: str) -> list[_dict]:
	"""Return validated rules for the given view name."""
	if not display_name:
		return []
	rows = cast(
		list[_dict],
		frappe.get_all(
			"Address Map Rule",
			filters={"parent": "Address Map Settings", "display_name": display_name},
			fields=["name", "fieldname", "operator", "value", "color", "shape", "hide_marker", "legend_label"],
			order_by="idx",
		),
	)
	if not rows:
		return []
	meta = frappe.get_meta(doctype)
	_allowed_ops = frozenset({"=", "!=", ">", "<", ">=", "<=", "like", "not like", "in", "not in", "is set", "is not set"})
	safe: list[_dict] = []
	for row in rows:
		hide = bool(row.get("hide_marker"))
		raw_fn = str(row.fieldname or "").strip()
		op = str(row.operator or "").strip()

		# Dot-notation: traverse a Link or Table field (e.g. "customer.territory" or "service_technicians.technician")
		if "." in raw_fn:
			parts = raw_fn.split(".", 1)
			link_field, linked_docfield = parts[0].strip(), parts[1].strip()
			if not link_field or not linked_docfield:
				continue
			link_meta_field = meta.get_field(link_field)
			if not link_meta_field or not link_meta_field.options:
				continue
			if link_meta_field.fieldtype in ("Table", "Table MultiSelect"):
				child_doctype = link_meta_field.options
				if not frappe.get_meta(child_doctype).get_field(linked_docfield):
					continue
				if not hide and (not op or op not in _allowed_ops):
					continue
				row.is_child_table_rule = True
				row.table_field = link_field
				row.child_doctype = child_doctype
				row.child_fieldname = linked_docfield
				safe.append(row)
				continue
			if link_meta_field.fieldtype != "Link":
				continue
			linked_doctype = link_meta_field.options
			if not frappe.get_meta(linked_doctype).get_field(linked_docfield):
				continue
			if not hide and (not op or op not in _allowed_ops):
				continue
			row.is_linked = True
			row.link_field = link_field
			row.linked_doctype = linked_doctype
			row.linked_fieldname = linked_docfield
			safe.append(row)
			continue

		# Regular fieldname (no dot)
		if hide:
			safe.append(row)  # hide rules need no field/op validation
			continue
		fn = _resolve_fieldname(raw_fn)
		if not fn or not op or op not in _allowed_ops:
			continue
		if fn not in _system_fields and not meta.get_field(fn):
			continue
		row.fieldname = fn  # store resolved name for _build_marker_map
		safe.append(row)
	return safe


# Friendly aliases → actual Frappe system field names
_FIELD_ALIASES: dict[str, str] = {
	"assigned": "_assign",
	"comments": "_comments",
	"liked_by": "_liked_by",
	"seen": "_seen",
}
_system_fields = frozenset({"name", "owner", "modified_by", "_assign", "_comments", "_liked_by", "_seen"})


def _resolve_fieldname(fn: str) -> str:
	"""Translate a friendly alias to the real field name, or return fn unchanged."""
	return _FIELD_ALIASES.get(fn, fn)


_me_field_re = re.compile(r"\{me\.([^}]+)\}")

def _resolve_me(val: str) -> str:
	"""Replace {me} and {me.fieldname} placeholders with the current user's values.

	- ``{me}`` → ``frappe.session.user`` (the user's email / login name)
	- ``{me.fieldname}`` → value of that field on the current User document
	  (e.g. ``{me.full_name}``, ``{me.employee}``)
	"""
	if "{me" not in val:
		return val
	user = frappe.session.user or ""
	# Replace {me.fieldname} first so the bare {me} pass doesn't interfere
	def _sub(m: re.Match) -> str:
		field = m.group(1).strip()
		if not field or not user:
			return ""
		try:
			return str(frappe.db.get_value("User", user, field) or "")
		except Exception:
			return ""
	val = _me_field_re.sub(_sub, val)
	val = val.replace("{me}", user)
	return val


def _build_marker_map(doctype: str, link_names: list[str], rules: list[_dict]) -> dict[str, dict]:
	"""Return a mapping of link_name → {color, shape, hide} based on first-matching rule."""
	if not rules or not link_names:
		return {}
	marker_map: dict[str, dict] = {}
	for rule in rules:
		unresolved = [n for n in link_names if n not in marker_map]
		if not unresolved:
			break
		hide = bool(rule.get("hide_marker"))
		op = str(rule.operator or "").strip()
		val = str(rule.value or "").strip()
		color = str(rule.color or "").strip()
		shape = str(rule.shape or "").strip()

		if rule.get("is_child_table_rule"):
			# Dot-notation rule: match via a child table field (e.g. "service_technicians.technician")
			table_field = str(rule.get("table_field") or "")
			child_doctype = str(rule.get("child_doctype") or "")
			child_fn = str(rule.get("child_fieldname") or "")
			if not hide and not color:
				continue
			try:
				if op == "is set":
					op, val = "is", "set"
				elif op == "is not set":
					op, val = "is", "not set"
				elif op in ("in", "not in"):
					val = [v.strip() for v in val.split(",") if v.strip()]  # type: ignore[assignment]
				if isinstance(val, str):
					val = _resolve_me(val)

				if table_field and child_doctype and child_fn and op:
					matching_parents = cast(list[str], frappe.get_all(
						child_doctype,
						filters=[
							["parent", "in", unresolved],
							["parentfield", "=", table_field],
							[child_fn, op, val],  # type: ignore[arg-type]
						],
						pluck="parent",
					))
					matched = set(matching_parents)
				elif hide:
					matched = set(unresolved)
				else:
					continue
			except Exception:
				continue
		elif rule.get("is_linked"):
			# Dot-notation rule: two-step query via a Link field
			link_field = str(rule.get("link_field") or "")
			linked_doctype = str(rule.get("linked_doctype") or "")
			linked_fn = str(rule.get("linked_fieldname") or "")
			if not hide and not color:
				continue
			try:
				# Normalise operator/value
				if op == "is set":
					op, val = "is", "set"
				elif op == "is not set":
					op, val = "is", "not set"
				elif op in ("in", "not in"):
					val = [v.strip() for v in val.split(",") if v.strip()]  # type: ignore[assignment]
				if isinstance(val, str):
					val = _resolve_me(val)

				if link_field and linked_doctype and linked_fn and op:
					linked_matched = cast(list[str], frappe.db.get_all(
						linked_doctype,
						filters=[[linked_fn, op, val]],  # type: ignore[arg-type]
						pluck="name",
					))
					if linked_matched:
						matched = set(cast(list[str], frappe.db.get_all(
							doctype,
							filters=[["name", "in", unresolved], [link_field, "in", linked_matched]],  # type: ignore[arg-type]
							pluck="name",
						)))
					else:
						matched = set()
				elif hide:
					matched = set(unresolved)  # no condition on a hide rule → hide all
				else:
					continue
			except Exception:
				continue
		else:
			fn = _resolve_fieldname(str(rule.fieldname or "").strip())
			if not hide and (not fn or not color):
				continue
			try:
				# Normalise operator/value to what frappe.db.get_all expects
				if op == "is set":
					op, val = "is", "set"
				elif op == "is not set":
					op, val = "is", "not set"
				elif op in ("in", "not in"):
					val = [v.strip() for v in val.split(",") if v.strip()]  # type: ignore[assignment]
				# {me} / {me.fieldname} placeholder → current session user (or a field thereof)
				if isinstance(val, str):
					val = _resolve_me(val)

				if fn and op:
					filter_entry: list | None = [fn, op, val]
				else:
					filter_entry = None
				if filter_entry is not None:
					matched = set(cast(list[str], frappe.db.get_all(
						doctype,
						filters=[["name", "in", unresolved], filter_entry],  # type: ignore[arg-type]
						pluck="name",
					)))
				elif hide:
					matched = set(unresolved)  # no condition on a hide rule → hide all
				else:
					continue
			except Exception:
				continue

		for name in matched:
			if name not in marker_map:
				if hide:
					marker_map[name] = {"hide": True}
				else:
					marker_map[name] = {"color": color, "shape": shape or None}
	return marker_map


def _get_address_type_priority(display_name: str | None) -> list[str]:
	"""Return the ordered address type list for the given view (from the comma-separated field).

	Returns an empty list when no priority is configured, which signals _pick_best_address
	to fall back to the default Frappe address type order.
	"""
	if not display_name:
		return []
	rows = frappe.get_all(
		"Address Map View",
		filters={"parent": "Address Map Settings", "parentfield": "doctypes", "display_name": display_name},
		fields=["address_type_priority"],
		limit=1,
	)
	if not rows:
		return []
	raw = str(rows[0].get("address_type_priority") or "").strip()
	return [t.strip() for t in raw.split(",") if t.strip()]


# Default address type order taken from the Frappe Address doctype Select options.
_DEFAULT_ADDRESS_TYPE_PRIORITY: tuple[str, ...] = (
	"Billing", "Shipping", "Office", "Personal", "Plant", "Postal",
	"Shop", "Subsidiary", "Warehouse", "Current", "Permanent", "Other",
)


def _pick_best_address(rows: list[_dict], priority: list[str] | None) -> list[_dict]:
	"""Keep only one address per link_name according to the priority list.

	The effective priority is built as:
	  1. User-configured types (from *priority*), in the given order.
	  2. Remaining default Frappe types not already listed, in default order.
	  3. Any other address types not covered above (unknown custom types), as
	     a last-resort fallback so every document always gets exactly one pin.
	"""
	user_list: list[str] = priority or []
	user_set = set(user_list)
	# Append default types not already in the user list
	effective: list[str] = list(user_list) + [t for t in _DEFAULT_ADDRESS_TYPE_PRIORITY if t not in user_set]
	priority_index: dict[str, int] = {t: i for i, t in enumerate(effective)}
	best: dict[str, _dict] = {}
	for row in rows:
		name = str(row.link_name or "")
		addr_type = str(row.address_type or "")
		rank = priority_index.get(addr_type)
		if rank is None:
			# Unknown custom type: use as fallback only if we have nothing yet
			if name not in best:
				best[name] = row
			continue
		current_rank = priority_index.get(str(best[name].address_type or ""), len(effective)) if name in best else len(effective)
		if rank < current_rank:
			best[name] = row
	# Return in original row order, one per link_name
	seen: set[str] = set()
	result: list[_dict] = []
	for row in rows:
		name = str(row.link_name or "")
		if name not in seen and best.get(name) is row:
			seen.add(name)
			result.append(row)
	return result


def _get_popup_line_fields(display_name: str | None, doctype: str) -> dict:
	"""Return validated popup_line1_field / popup_line2_field for the given view.

	Supports dot-notation (e.g. "customer.customer_name") to traverse a single
	Link field on the source doctype.
	"""
	if not display_name:
		return {}
	rows = frappe.get_all(
		"Address Map View",
		filters={"parent": "Address Map Settings", "parentfield": "doctypes", "display_name": display_name},
		fields=["popup_line1_field", "popup_line2_field"],
		limit=1,
	)
	if not rows:
		return {}
	row = rows[0]
	meta = frappe.get_meta(doctype)
	result: dict = {}
	for key in ("popup_line1_field", "popup_line2_field"):
		fn = str(row.get(key) or "").strip()
		if not fn:
			continue
		if "." in fn:
			# Dot-notation: link_field.linked_docfield
			link_field, linked_docfield = fn.split(".", 1)
			link_field, linked_docfield = link_field.strip(), linked_docfield.strip()
			if not link_field or not linked_docfield:
				continue
			link_meta_field = meta.get_field(link_field)
			if not link_meta_field or link_meta_field.fieldtype != "Link" or not link_meta_field.options:
				continue
			linked_doctype = link_meta_field.options
			if not frappe.get_meta(linked_doctype).get_field(linked_docfield):
				continue
			result[key] = {
				"fieldname": fn,
				"is_linked": True,
				"link_field": link_field,
				"linked_doctype": linked_doctype,
				"linked_docfield": linked_docfield,
			}
		else:
			if meta.get_field(fn):
				result[key] = {"fieldname": fn, "is_linked": False}
	return result


def _get_popup_fields(display_name: str | None, doctype: str) -> list[dict]:
	"""Return validated popup field configs for the given view from settings."""
	if not display_name:
		return []
	rows = frappe.get_all(
		"Address Map Popup Field",
		filters={"parent": "Address Map Settings", "view_name": display_name},
		fields=["fieldname", "label"],
		order_by="idx",
	)
	if not rows:
		return []
	meta = frappe.get_meta(doctype)
	safe: list[dict] = []
	for row in rows:
		fn = str(row.get("fieldname") or "").strip()
		lbl = str(row.get("label") or "").strip()
		if not fn:
			continue

		# Dot-notation: traverse a Link field (e.g. "customer.territory")
		# or a child Table field (e.g. "service_technicians.technician").
		if "." in fn:
			parts = fn.split(".", 1)
			link_field, linked_docfield = parts[0].strip(), parts[1].strip()
			if not link_field or not linked_docfield:
				continue
			link_meta_field = meta.get_field(link_field)
			if not link_meta_field:
				continue

			# Child table dot-notation
			if link_meta_field.fieldtype in ("Table", "Table MultiSelect"):
				child_doctype = link_meta_field.options
				child_field_meta = frappe.get_meta(child_doctype).get_field(linked_docfield)
				if not child_field_meta or child_field_meta.fieldtype in _POPUP_SKIP_TYPES:
					continue
				safe.append({
					"fieldname": fn,
					"label": lbl or child_field_meta.label or fn,
					"fieldtype": child_field_meta.fieldtype,
					"is_child_table": True,
					"table_field": link_field,
					"child_doctype": child_doctype,
					"child_field": linked_docfield,
				})
				continue

			# Link field dot-notation
			if link_meta_field.fieldtype != "Link" or not link_meta_field.options:
				continue
			linked_doctype = link_meta_field.options
			linked_field = frappe.get_meta(linked_doctype).get_field(linked_docfield)
			if not linked_field or linked_field.fieldtype in _POPUP_SKIP_TYPES:
				continue
			safe.append({
				"fieldname": fn,
				"label": lbl or linked_field.label or fn,
				"fieldtype": linked_field.fieldtype,
				"is_linked": True,
				"link_field": link_field,
				"linked_doctype": linked_doctype,
				"linked_docfield": linked_docfield,
			})
			continue

		# Regular fieldname (no dot)
		field = meta.get_field(fn)
		if not field or field.fieldtype in _POPUP_SKIP_TYPES:
			continue
		safe.append({"fieldname": fn, "label": lbl or field.label or fn, "fieldtype": field.fieldtype})
	return safe


def _enrich_rows(rows: list[_dict], doctype: str, popup_fields: list[dict] | None, line_fields: dict | None = None) -> None:
	"""Bulk-fetch configured popup fields and popup line fields for all rows and merge in-place."""
	names = [str(r.link_name) for r in rows if r.link_name]
	if not names:
		return

	# Separate regular, linked (dot-notation Link), and child table popup fields
	regular_popup = [f for f in (popup_fields or []) if not f.get("is_linked") and not f.get("is_child_table")]
	linked_popup = [f for f in (popup_fields or []) if f.get("is_linked")]
	child_table_popup = [f for f in (popup_fields or []) if f.get("is_child_table")]

	# Columns to fetch from the main doctype: regular popup fieldnames + line fields +
	# link_field columns required by linked popup fields
	main_fieldnames: list[str] = [f["fieldname"] for f in regular_popup]
	for key in ("popup_line1_field", "popup_line2_field"):
		line_cfg = (line_fields or {}).get(key)
		if not line_cfg:
			continue
		if isinstance(line_cfg, dict):
			# new dict format
			if line_cfg.get("is_linked"):
				lf = line_cfg["link_field"]
				if lf not in main_fieldnames:
					main_fieldnames.append(lf)
			else:
				fn = line_cfg["fieldname"]
				if fn not in main_fieldnames:
					main_fieldnames.append(fn)
		else:
			# legacy plain string
			if line_cfg not in main_fieldnames:
				main_fieldnames.append(line_cfg)
	for f in linked_popup:
		lf = f["link_field"]
		if lf not in main_fieldnames:
			main_fieldnames.append(lf)

	doc_map: dict[str, _dict] = {}
	if main_fieldnames:
		docs = cast(
			list[_dict],
			frappe.get_all(
				doctype,
				filters=[["name", "in", names]],
				fields=["name"] + main_fieldnames,
			),
		)
		doc_map = {str(d.name): d for d in docs}

	# Enrich regular popup fields and line fields
	for row in rows:
		extra = doc_map.get(str(row.link_name or ""), _dict())
		for f in regular_popup:
			row[f"_extra_{f['fieldname']}"] = extra.get(f["fieldname"], "")
		for key, prefix in (("popup_line1_field", "_popup_line1"), ("popup_line2_field", "_popup_line2")):
			line_cfg = (line_fields or {}).get(key)
			if not line_cfg:
				continue
			if isinstance(line_cfg, dict) and not line_cfg.get("is_linked"):
				row[prefix] = extra.get(line_cfg["fieldname"], "")
			elif isinstance(line_cfg, str):
				# legacy plain string
				row[prefix] = extra.get(line_cfg, "")

	# Enrich all linked fields (popup fields + line1/line2) in one unified pass.
	# Linked line fields are treated identically to linked popup fields; the only
	# difference is the destination key on the row (_popup_line1/2 vs _extra_*).
	unified_linked: list[dict] = list(linked_popup)
	for key, dest_key in (("popup_line1_field", "_popup_line1"), ("popup_line2_field", "_popup_line2")):
		line_cfg = (line_fields or {}).get(key)
		if isinstance(line_cfg, dict) and line_cfg.get("is_linked"):
			unified_linked.append({**line_cfg, "_dest_key": dest_key, "_dest_key_link": dest_key + "_link"})

	if unified_linked and doc_map:
		groups: dict[tuple[str, str], list[dict]] = {}
		for f in unified_linked:
			groups.setdefault((f["link_field"], f["linked_doctype"]), []).append(f)

		for (link_field, linked_doctype), fields in groups.items():
			link_values: set[str] = set()
			for row in rows:
				lv = str(doc_map.get(str(row.link_name or ""), _dict()).get(link_field) or "").strip()
				if lv:
					link_values.add(lv)
			if not link_values:
				continue
			linked_fieldnames = list({f["linked_docfield"] for f in fields})
			linked_docs = cast(
				list[_dict],
				frappe.get_all(
					linked_doctype,
					filters=[["name", "in", list(link_values)]],
					fields=["name"] + linked_fieldnames,
				),
			)
			linked_doc_map = {str(d.name): d for d in linked_docs}
			for row in rows:
				lv = str(doc_map.get(str(row.link_name or ""), _dict()).get(link_field) or "").strip()
				linked_data = linked_doc_map.get(lv, _dict()) if lv else _dict()
				for f in fields:
					dest = f.get("_dest_key") or f"_extra_{f['fieldname']}"
					row[dest] = linked_data.get(f["linked_docfield"], "")
					dest_link = f.get("_dest_key_link")
					if dest_link:
						row[dest_link] = lv

	# Enrich child table fields: batch-fetch child rows grouped by parent.
	if child_table_popup and names:
		ct_groups: dict[tuple[str, str], list[dict]] = {}
		for f in child_table_popup:
			ct_groups.setdefault((f["table_field"], f["child_doctype"]), []).append(f)

		for (table_field, child_doctype), fields in ct_groups.items():
			child_fieldnames = list({f["child_field"] for f in fields})
			child_rows = cast(
				list[_dict],
				frappe.get_all(
					child_doctype,
					filters=[["parent", "in", names], ["parentfield", "=", table_field]],
					fields=["parent"] + child_fieldnames,
					order_by="idx",
				),
			)
			child_by_parent: dict[str, list[_dict]] = {}
			for cr in child_rows:
				child_by_parent.setdefault(str(cr.parent or ""), []).append(cr)
			for row in rows:
				parent_name = str(row.link_name or "")
				child_data = child_by_parent.get(parent_name, [])
				for f in fields:
					values = [
						str(cr.get(f["child_field"]) or "")
						for cr in child_data
						if cr.get(f["child_field"])
					]
					row[f"_extra_{f['fieldname']}"] = values


def _to_feature(row: _dict, doctype: str, popup_fields: list[dict] | None = None, line_fields: dict | None = None, marker_map: dict | None = None) -> dict:
	address_html = _format_address(row)
	link_name = str(row.link_name or "")
	link_title = str(row.link_title or link_name)

	# Determine assignment server-side where frappe.session.user is authoritative.
	# Only _assign (explicit "Assign To") is checked, not owner (creator).
	me = frappe.session.user or ""
	try:
		assigned = json.loads(str(row._assign or "") or "[]")
	except (ValueError, TypeError):
		assigned = []
	is_mine = bool(me and me in assigned)

	line1 = str(row.get("_popup_line1") or "").strip() or link_name
	line2 = str(row.get("_popup_line2") or "").strip() or link_title

	line1_cfg = (line_fields or {}).get("popup_line1_field")
	line2_cfg = (line_fields or {}).get("popup_line2_field")
	dt_slug = frappe.scrub(doctype).replace("_", "-")
	main_url = f"/app/{dt_slug}/{urllib.parse.quote(link_name)}"

	# Line 1: always a link — to the linked doc if dot-notation, otherwise main doc
	if isinstance(line1_cfg, dict) and line1_cfg.get("is_linked"):
		l1_name = str(row.get("_popup_line1_link") or "").strip()
		l1_slug = frappe.scrub(line1_cfg["linked_doctype"]).replace("_", "-")
		line1_url = f"/app/{l1_slug}/{urllib.parse.quote(l1_name)}" if l1_name else main_url
	else:
		line1_url = main_url

	# Line 2: always a link — to the linked doc if dot-notation, otherwise main doc
	if isinstance(line2_cfg, dict) and line2_cfg.get("is_linked"):
		l2_name = str(row.get("_popup_line2_link") or "").strip()
		l2_slug = frappe.scrub(line2_cfg["linked_doctype"]).replace("_", "-")
		line2_url = f"/app/{l2_slug}/{urllib.parse.quote(l2_name)}" if l2_name else main_url
	else:
		line2_url = main_url

	line2_html = f'<a href="{line2_url}" target="_blank">{escape_html(line2)}</a>'

	popup_html = (
		f'<strong><a href="{line1_url}" target="_blank">{escape_html(line1)}</a></strong><br>'
		f'{line2_html}'
		f'<hr style="margin:4px 0">{address_html}'
	)
	if popup_fields:
		extra_rows = ""
		for f in popup_fields:
			val = row.get(f"_extra_{f['fieldname']}", "")
			is_check = f.get("fieldtype") == "Check"
			if f.get("is_child_table"):
				items = [str(v) for v in (val if isinstance(val, list) else []) if v is not None and str(v).strip()]
				if items:
					lbl = escape_html(str(f.get("label") or f["fieldname"]))
					val_html = "<br>".join(escape_html(v) for v in items)
					extra_rows += (
						f'<tr>'
						f'<td style="color:var(--text-muted);padding:1px 4px 1px 0;white-space:nowrap;vertical-align:top">{lbl}</td>'
						f'<td style="padding:1px 0">{val_html}</td>'
						f'</tr>'
					)
				continue
			if is_check:
				# Always show checkboxes; render as ✓ or ✗
				lbl = escape_html(str(f.get("label") or f["fieldname"]))
				val_html = "&#10003;" if val else "&#10007;"
				color = "var(--green-500)" if val else "var(--red-500)"
				extra_rows += (
					f'<tr>'
					f'<td style="color:var(--text-muted);padding:1px 4px 1px 0;white-space:nowrap;vertical-align:top">{lbl}</td>'
					f'<td style="padding:1px 0;color:{color}">{val_html}</td>'
					f'</tr>'
				)
			elif val is not None and str(val).strip():
				lbl = escape_html(str(f.get("label") or f["fieldname"]))
				val_html = escape_html(str(val)).replace("\n", "<br>")
				extra_rows += (
					f'<tr>'
					f'<td style="color:var(--text-muted);padding:1px 4px 1px 0;white-space:nowrap;vertical-align:top">{lbl}</td>'
					f'<td style="padding:1px 0">{val_html}</td>'
					f'</tr>'
				)
		if extra_rows:
			popup_html += (
				f'<hr style="margin:4px 0">'
				f'<table style="width:100%;border-collapse:collapse;font-size:.85em">'
				f'{extra_rows}</table>'
			)
	color_override = (marker_map or {}).get(link_name) or {}
	return {
		"type": "Feature",
		"properties": {
			"name": link_name,
			"doctype": doctype,
			"popup": popup_html,
			"address_name": str(row.address_name or ""),
			"is_mine": is_mine,
			**({"color": color_override["color"]} if color_override.get("color") else {}),
			**({"shape": color_override["shape"].lower()} if color_override.get("shape") else {}),
		},
		"geometry": {
			"type": "Point",
			"coordinates": [float(row.longitude or 0), float(row.latitude or 0)],
		},
	}


def _format_address(row: _dict) -> str:
	"""Render the address using Frappe's country-specific Address Template.

	Templates are cached per country for the duration of the request so that a
	map with many markers of the same country only hits the DB once.
	"""
	from frappe.contacts.doctype.address.address import get_address_templates

	country = str(row.get("country") or "")

	# Per-request template cache keyed by country
	if not hasattr(frappe.local, "_address_template_cache"):
		frappe.local._address_template_cache = {}
	template_cache: dict = frappe.local._address_template_cache

	if country not in template_cache:
		try:
			result = get_address_templates({"country": country})  # type: ignore[func-returns-value]
			template_cache[country] = result[1] if result else None  # type: ignore[index]
		except Exception:
			template_cache[country] = None

	template = template_cache[country]

	address_dict = {
		"address_line1": str(row.get("address_line1") or ""),
		"address_line2": str(row.get("address_line2") or ""),
		"city": str(row.get("city") or ""),
		"state": str(row.get("state") or ""),
		"pincode": str(row.get("pincode") or ""),
		"country": country,
		"phone": "",
		"fax": "",
		"email_id": "",
	}

	if template:
		try:
			rendered = frappe.render_template(template, address_dict) or ""
			# Strip trailing blank <br> tags the default template appends
			rendered = re.sub(r"(\s*<br\s*/?>)+\s*$", "", rendered, flags=re.IGNORECASE).strip()
			return rendered
		except Exception:
			pass

	# Fallback: simple line-per-field
	parts = [
		escape_html(address_dict["address_line1"]),
		escape_html(address_dict["address_line2"]),
		escape_html(address_dict["city"]),
		escape_html(address_dict["state"]),
		escape_html(address_dict["pincode"]),
		escape_html(country),
	]
	return "<br>".join(p for p in parts if p)


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

@frappe.whitelist()
def get_settings() -> dict:
	"""Return map settings for the frontend (default doctype, location marker)."""
	s = frappe.get_cached_doc("Address Map Settings")
	return {
		"default_doctype": s.get("default_doctype") or None,
		"default_marker_color": s.get("default_marker_color") or "#3388ff",
		"default_marker_shape": str(s.get("default_marker_shape") or "Circle").lower(),  # type: ignore[union-attr]
		"location_color": s.get("location_color") or "#4a90e2",
		"location_shape": str(s.get("location_shape") or "Circle").lower(),  # type: ignore[union-attr]
	}


# ---------------------------------------------------------------------------
# Assignment helpers
# ---------------------------------------------------------------------------

@frappe.whitelist()
def assign_to_me(doctype: str, name: str) -> None:
	"""Remove all current assignees from `doctype`/`name` then assign to the current user."""
	from frappe.desk.form.assign_to import add as _assign_add, clear as _assign_clear

	frappe.get_doc(doctype, name).check_permission("write")

	# Remove every existing open/pending assignment
	_assign_clear(doctype, name, ignore_permissions=True)

	# Add current user as the sole assignee
	_assign_add({"doctype": doctype, "name": name, "assign_to": [frappe.session.user]}, ignore_permissions=True)


# ---------------------------------------------------------------------------
# Fixed pins (Address Map Settings)
# ---------------------------------------------------------------------------

@frappe.whitelist()
def get_pinned_addresses() -> dict:
	"""Return the fixed-pin addresses configured in Address Map Settings as GeoJSON.

	Read access is not required on Address Map Settings; any authenticated user
	who can open the map page can see the pinned addresses.
	"""
	pins = cast(
		list[_dict],
		frappe.db.get_all(
			"Address Map Pin",
			filters={"parent": "Address Map Settings", "parenttype": "Address Map Settings"},
			fields=["address", "label", "color", "shape"],
			order_by="idx",
		),
	)

	features = []
	for pin in pins:
		if not pin.address:
			continue

		addr = cast(
			_dict,
			frappe.db.get_value(
				"Address",
				pin.address,
				["name", "address_line1", "address_line2", "city", "state", "country", "pincode", "latitude", "longitude"],  # type: ignore[arg-type]
				as_dict=True,
			),
		)
		if not addr or not addr.latitude or not addr.longitude:
			continue

		label = str(pin.label or addr.name or "")
		color = str(pin.color or "#e74c3c")
		shape = str(pin.shape or "Circle")
		addr_name = str(addr.name or "")
		popup_html = f'<strong>{escape_html(label)}</strong>'
		features.append(
			{
				"type": "Feature",
				"properties": {
					"name": addr_name,
					"label": label,
					"color": color,
					"shape": shape,
					"popup": popup_html,
					"is_pinned": True,
				},
				"geometry": {
					"type": "Point",
					"coordinates": [float(addr.longitude), float(addr.latitude)],
				},
			}
		)

	return {"type": "FeatureCollection", "features": features}


# ---------------------------------------------------------------------------
# Bulk geocoding
# ---------------------------------------------------------------------------

@frappe.whitelist()
def geocode_all_addresses() -> dict:
	"""Enqueue a background job that geocodes all addresses missing coordinates.

	Returns {"queued": <count>} so the caller can show a meaningful message.
	Only System Managers may trigger this (Nominatim rate-limit concern).
	"""
	frappe.only_for("System Manager")

	names = cast(
		list[str],
		frappe.db.get_all(
			"Address",
			filters=[["latitude", "in", ["", "0", None]]],
			pluck="name",
		),
	)
	count = len(names)
	if count:
		frappe.enqueue(
			"address_map.geocoding.geocode_all_job",
			names=names,
			queue="long",
			timeout=3600,
			job_id="address_map_geocode_all",
			deduplicate=True,
		)
	return {"queued": count}
