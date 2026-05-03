// Copyright (c) 2026, ALITECS Alexander Leisentritt and contributors
// License: GPLv3

frappe.ui.form.on("Address", {
	refresh(frm) {
		frm.add_custom_button(__("Koordinaten einfügen"), () => {
			show_coordinate_dialog(frm);
		}, __("Geocoordinates"));
	},
});

/**
 * Try to parse a coordinate string in various formats:
 *
 * Decimal formats (most common from Google Maps / Apple Maps copy):
 *   "48.1372982, 11.5755387"
 *   "48.1372982,11.5755387"
 *   "@48.1372982,11.5755387"        (Google Maps URL fragment)
 *   "48.1372982° N, 11.5755387° E"
 *
 * DMS (degrees/minutes/seconds):
 *   "48°30'12\"N, 11°45'30\"E"
 *   "48° 30' 12\" N, 11° 45' 30\" E"
 *   "N48°30'12\", E11°45'30\""
 *
 * Returns { lat, lng } as numbers or null if unparseable.
 */
function parse_coordinates(raw) {
	const s = (raw || "").trim();
	if (!s) return null;

	// ── 1. Plain decimal (with optional leading @ and optional N/S/E/W) ──────
	const dec = /^@?\s*(-?\d{1,3}(?:[.,]\d+)?)\s*°?\s*([NS])?\s*[,;\s]+\s*(-?\d{1,3}(?:[.,]\d+)?)\s*°?\s*([EW])?$/i;
	let m = s.match(dec);
	if (m) {
		let lat = parseFloat(m[1].replace(",", "."));
		let lng = parseFloat(m[3].replace(",", "."));
		if (m[2] && m[2].toUpperCase() === "S") lat = -Math.abs(lat);
		if (m[4] && m[4].toUpperCase() === "W") lng = -Math.abs(lng);
		if (is_valid(lat, lng)) return { lat, lng };
	}

	// ── 2. DMS – "48°30'12\"N, 11°45'30\"E" ─────────────────────────────────
	const dms_part = String.raw`(\d{1,3})\s*[°d]\s*(\d{1,2})\s*[\'′]\s*(\d{1,2}(?:[.,]\d+)?)\s*[\"″]?\s*([NSEW])`;
	const dms = new RegExp(`^${dms_part}[,;\\s]+${dms_part}$`, "i");
	m = s.match(dms);
	if (m) {
		const to_dec = (deg, min, sec, dir) => {
			let v = parseInt(deg) + parseInt(min) / 60 + parseFloat(sec.replace(",", ".")) / 3600;
			if ("SW".includes(dir.toUpperCase())) v = -v;
			return v;
		};
		const a = to_dec(m[1], m[2], m[3], m[4]);
		const b = to_dec(m[5], m[6], m[7], m[8]);
		// Decide which is lat and which is lng by direction letter
		let lat, lng;
		if ("NS".includes(m[4].toUpperCase())) { lat = a; lng = b; }
		else { lat = b; lng = a; }
		if (is_valid(lat, lng)) return { lat, lng };
	}

	// ── 3. DMS without explicit N/S/E/W – assume first=lat, second=lng ───────
	const dms2_part = String.raw`(\d{1,3})\s*[°d]\s*(\d{1,2})\s*[\'′]\s*(\d{1,2}(?:[.,]\d+)?)\s*[\"″]?`;
	const dms2 = new RegExp(`^${dms2_part}[,;\\s]+${dms2_part}$`, "i");
	m = s.match(dms2);
	if (m) {
		const to_dec = (deg, min, sec) =>
			parseInt(deg) + parseInt(min) / 60 + parseFloat(sec.replace(",", ".")) / 3600;
		const lat = to_dec(m[1], m[2], m[3]);
		const lng = to_dec(m[4], m[5], m[6]);
		if (is_valid(lat, lng)) return { lat, lng };
	}

	return null;
}

function is_valid(lat, lng) {
	return (
		isFinite(lat) && isFinite(lng) &&
		lat >= -90 && lat <= 90 &&
		lng >= -180 && lng <= 180
	);
}

function show_coordinate_dialog(frm) {
	const d = new frappe.ui.Dialog({
		title: __("Koordinaten einfügen"),
		fields: [
			{
				fieldtype: "HTML",
				options: `<p class="text-muted small">
					${__("Koordinaten aus Google Maps oder Apple Maps einfügen.")}
					<br>${__("Unterstützte Formate:")}
					<br><code>48.1372982, 11.5755387</code>
					<br><code>48°30'12\"N, 11°45'30\"E</code>
				</p>`,
			},
			{
				fieldtype: "Data",
				fieldname: "coordinates",
				label: __("Koordinaten"),
				reqd: 1,
				description: __("Strg+V / ⌘V zum Einfügen"),
			},
			{
				fieldtype: "Section Break",
				fieldname: "preview_section",
				label: __("Vorschau"),
				depends_on: "eval:doc.coordinates",
			},
			{
				fieldtype: "HTML",
				fieldname: "preview_html",
				depends_on: "eval:doc.coordinates",
			},
		],
		primary_action_label: __("Übernehmen"),
		primary_action({ coordinates }) {
			const result = parse_coordinates(coordinates);
			if (!result) {
				frappe.msgprint({
					title: __("Ungültiges Format"),
					message: __("Die eingegebenen Koordinaten konnten nicht erkannt werden."),
					indicator: "red",
				});
				return;
			}
			frm.set_value("latitude", flt(result.lat, 8));
			frm.set_value("longitude", flt(result.lng, 8));
			d.hide();
			frappe.show_alert({
				message: __("Koordinaten übernommen: {0}, {1}", [result.lat.toFixed(7), result.lng.toFixed(7)]),
				indicator: "green",
			});
		},
	});

	// Live-Vorschau beim Tippen/Einfügen
	d.fields_dict.coordinates.df.onchange = function () {
		const raw = d.get_value("coordinates");
		const result = parse_coordinates(raw);
		const html_field = d.fields_dict.preview_html;
		if (!raw) {
			html_field.$wrapper.empty();
			return;
		}
		if (result) {
			html_field.$wrapper.html(
				`<div class="alert alert-success p-2 small">
					<b>${__("Breitengrad")}:</b> ${result.lat.toFixed(8)}<br>
					<b>${__("Längengrad")}:</b> ${result.lng.toFixed(8)}
				</div>`
			);
		} else {
			html_field.$wrapper.html(
				`<div class="alert alert-danger p-2 small">
					${__("Format nicht erkannt")}
				</div>`
			);
		}
	};

	d.show();
	// Fokus ins Eingabefeld setzen damit man sofort einfügen kann
	setTimeout(() => d.fields_dict.coordinates.input && d.fields_dict.coordinates.input.focus(), 200);
}
