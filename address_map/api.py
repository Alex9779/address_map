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
	rows = cast(
		list[_dict],
		frappe.db.get_all(
			"Address Map View",
			filters={"parent": "Address Map Settings", "parentfield": "doctypes"},
			fields=["doctype_name", "via_doctype", "display_name", "allow_assign", "address_type_priority"],
			order_by="idx",
		),
	)

	result = []
	for row in rows:
		doctype = str(row.doctype_name or "").strip()
		via = str(row.via_doctype or "").strip() or None
		if not doctype:
			continue
		via_field = None
		if via:
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
		if hide:
			safe.append(row)  # hide rules need no field/op validation
			continue
		fn = _resolve_fieldname(str(row.fieldname or "").strip())
		op = str(row.operator or "").strip()
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
		fn = _resolve_fieldname(str(rule.fieldname or "").strip())
		op = str(rule.operator or "").strip()
		val = str(rule.value or "").strip()
		color = str(rule.color or "").strip()
		shape = str(rule.shape or "").strip()
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
			# {me} placeholder → current session user
			if isinstance(val, str) and "{me}" in val:
				val = val.replace("{me}", frappe.session.user or "")

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

	When *priority* is set (user-configured list):
	  Only addresses whose type appears in the list are considered.
	  Documents with no matching address type are omitted.

	When *priority* is empty (field left blank):
	  Uses _DEFAULT_ADDRESS_TYPE_PRIORITY to pick the best type.
	  Falls back to the very first address found if none match any default type,
	  so every document always gets exactly one pin.
	"""
	strict = bool(priority)  # True → user-configured, False → use defaults
	effective: list[str] = priority if strict else list(_DEFAULT_ADDRESS_TYPE_PRIORITY)
	priority_index: dict[str, int] = {t: i for i, t in enumerate(effective)}
	best: dict[str, _dict] = {}
	for row in rows:
		name = str(row.link_name or "")
		addr_type = str(row.address_type or "")
		rank = priority_index.get(addr_type)
		if rank is None:
			if strict:
				continue  # type not in user list — skip entirely
			# non-strict: use as fallback only if we have nothing yet
			if name not in best:
				best[name] = row
			continue
		if name not in best or rank < priority_index.get(str(best[name].address_type or ""), len(effective)):
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
	"""Return validated popup_line1_field / popup_line2_field for the given view."""
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
		if fn and meta.get_field(fn):
			result[key] = fn
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
	fieldnames: list[str] = [f["fieldname"] for f in (popup_fields or [])]
	for key in ("popup_line1_field", "popup_line2_field"):
		fn = (line_fields or {}).get(key)
		if fn and fn not in fieldnames:
			fieldnames.append(fn)
	if not fieldnames:
		return
	docs = cast(
		list[_dict],
		frappe.get_all(
			doctype,
			filters=[["name", "in", names]],
			fields=["name"] + fieldnames,
		),
	)
	doc_map = {str(d.name): d for d in docs}
	for row in rows:
		extra = doc_map.get(str(row.link_name or ""), _dict())
		for f in (popup_fields or []):
			row[f"_extra_{f['fieldname']}"] = extra.get(f["fieldname"], "")
		for key, prefix in (("popup_line1_field", "_popup_line1"), ("popup_line2_field", "_popup_line2")):
			fn = (line_fields or {}).get(key)
			if fn:
				row[prefix] = extra.get(fn, "")


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

	popup_html = (
		f'<strong>{escape_html(line1)}</strong><br>'
		f'<a href="/app/{frappe.scrub(doctype)}/{urllib.parse.quote(link_name)}" target="_blank">'
		f'{escape_html(line2)}</a>'
		f'<hr style="margin:4px 0">{address_html}'
	)
	if popup_fields:
		extra_rows = ""
		for f in popup_fields:
			val = row.get(f"_extra_{f['fieldname']}", "")
			is_check = f.get("fieldtype") == "Check"
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
