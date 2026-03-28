# Copyright (c) 2026, ALITECS Alexander Leisentritt and contributors
# License: GPLv3

import json
import urllib.parse
import urllib.request

import frappe

_DEFAULT_NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
_DEFAULT_USER_AGENT = "address_map/0.0.1 (info@alitecs.de)"
_ADDRESS_FIELDS = {"address_line1", "address_line2", "city", "state", "country", "pincode"}
_COORD_FIELDS = {"latitude", "longitude"}


def _get_nominatim_settings() -> tuple[str, str]:
	"""Return (nominatim_url, user_agent) from settings, falling back to defaults."""
	try:
		settings = frappe.get_cached_doc("Address Map Settings")
		url = str(settings.get("nominatim_url") or _DEFAULT_NOMINATIM_URL)
		agent = str(settings.get("nominatim_user_agent") or _DEFAULT_USER_AGENT)
		return url, agent
	except Exception:
		return _DEFAULT_NOMINATIM_URL, _DEFAULT_USER_AGENT


def on_address_update(doc, method=None):
	"""doc_events hook: geocode the address whenever relevant fields change.

	Priority:
	- Address text fields changed → re-geocode via Nominatim (overwrites coordinates).
	- Only lat/lng changed manually → rebuild geolocation JSON without calling Nominatim.
	- Nothing relevant changed → skip entirely.
	"""
	before = doc.get_doc_before_save()
	if before:
		addr_changed = any(getattr(doc, f) != getattr(before, f, None) for f in _ADDRESS_FIELDS)
		coord_changed = any(getattr(doc, f) != getattr(before, f, None) for f in _COORD_FIELDS)

		if addr_changed:
			# Address text changed: re-geocode; Nominatim result overwrites any manual values.
			result = _call_nominatim(doc)
			if result:
				_store_coordinates(doc, result["lat"], result["lon"])
		elif coord_changed and doc.latitude and doc.longitude:
			# User set coordinates manually: just rebuild the geolocation GeoJSON.
			_store_coordinates(doc, doc.latitude, doc.longitude)
	else:
		# New document: geocode unconditionally.
		result = _call_nominatim(doc)
		if result:
			_store_coordinates(doc, result["lat"], result["lon"])


def _build_query(address) -> str:
	"""Build a free-form query string from an Address document."""
	parts = [
		address.address_line1,
		address.address_line2,
		address.city,
		address.state,
		address.pincode,
		address.country,
	]
	return ", ".join(p for p in parts if p)


def _call_nominatim(address) -> dict | None:
	"""Call the Nominatim search API and return the first result dict, or None."""
	query = _build_query(address)
	if not query:
		return None

	nominatim_url, user_agent = _get_nominatim_settings()
	params = urllib.parse.urlencode({"q": query, "format": "json", "limit": "1"})
	url = f"{nominatim_url}?{params}"
	req = urllib.request.Request(url, headers={"User-Agent": user_agent})

	try:
		with urllib.request.urlopen(req, timeout=10) as resp:  # nosec B310 – URL is a constant
			data = json.loads(resp.read().decode())
			return data[0] if data else None
	except Exception:
		frappe.log_error(frappe.get_traceback(), f"address_map: Nominatim request failed for {address.name}")
		return None


def _store_coordinates(address, lat: str, lon: str):
	"""Write latitude, longitude and geolocation GeoJSON back to the Address document."""
	lat_f = float(lat)
	lon_f = float(lon)

	geolocation = json.dumps(
		{
			"type": "FeatureCollection",
			"features": [
				{
					"type": "Feature",
					"properties": {"name": address.name},
					"geometry": {"type": "Point", "coordinates": [lon_f, lat_f]},
				}
			],
		}
	)

	frappe.db.set_value(
		"Address",
		address.name,
		{"latitude": lat_f, "longitude": lon_f, "geolocation": geolocation},
		update_modified=False,
	)
	frappe.db.commit()


def geocode_all_job(names: list[str]) -> None:
	"""Background job: geocode every address in *names* that still lacks coordinates.

	Skips addresses that acquired coordinates since the job was enqueued.
	Logs errors per address without aborting the whole run.
	"""
	import time

	for name in names:
		try:
			# Re-check: another process may have geocoded it already
			if frappe.db.get_value("Address", name, "latitude"):
				continue

			doc = frappe.get_doc("Address", name)
			result = _call_nominatim(doc)
			if result:
				_store_coordinates(doc, result["lat"], result["lon"])

			# Nominatim usage policy: max 1 req/s
			time.sleep(1)
		except Exception:
			frappe.log_error(frappe.get_traceback(), f"address_map: geocode_all_job failed for {name}")
