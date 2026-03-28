// Copyright (c) 2026, ALITECS Alexander Leisentritt and contributors
// License: GPLv3

frappe.pages["address-map"].on_page_load = function (wrapper) {
	frappe.address_map_page = new AddressMapPage(wrapper);
};

frappe.pages["address-map"].on_page_show = function () {
	if (frappe.address_map_page) {
		frappe.address_map_page.refresh();
	}
};

class AddressMapPage {
	constructor(wrapper) {
		this.wrapper = wrapper;
		this.page = frappe.ui.make_app_page({
			parent: wrapper,
			title: __("Address Map"),
			single_column: false,
		});

		this._settings = {};

		// Grey icon-only refresh button left of the "…" menu (standard Frappe location)
		this.page.add_action_icon("es-line-reload", () => this.refresh(), "", __("Refresh"));

		// "Toggle Sidebar" in the kebab menu (same pattern as list view)
		this.page.add_menu_item(__("Toggle Sidebar"), () => this._toggle_sidebar(), true, "Ctrl+K");

		this._setup_sidebar();
		this._setup_filter_area();
		this._setup_map_container();
		this._show_or_hide_sidebar();

		frappe.call({ method: "address_map.api.get_settings" }).then((r) => {
			this._settings = r.message || {};
			this._init_map();
			this._load_views();
		});
	}

	// ──────────────────────────────────────────────────────────────
	// Sidebar
	// ──────────────────────────────────────────────────────────────

	_setup_sidebar() {
		this._$sidebar = $('<div class="list-sidebar overlay-sidebar hidden-xs hidden-sm"></div>').appendTo(
			this.page.sidebar.empty(),
		);

		// DocType selector
		this._$sidebar.append(`
<div class="sidebar-section">
<div class="list-filters">
<div class="sidebar-label text-muted small mb-1">${__("View")}</div>
<select id="address-map-doctype-select" class="form-control input-xs">
<option value="">${__("Select a view\u2026")}</option>
</select>
</div>
</div>
`);
		this.$doctype_select = this._$sidebar.find("#address-map-doctype-select");
		this.$doctype_select.on("change", () => this._on_doctype_change());

		// Saved filters section (hidden until a doctype is selected)
		this._$sidebar.append(`
<div class="sidebar-section" id="address-map-saved-filters" style="display:none;">
<div class="list-filters">
<div class="sidebar-label text-muted small mb-1">${__("Saved Filters")}</div>
<ul class="list-unstyled">
<li class="filter-input-area"></li>
<li class="sidebar-action mt-1">
<a class="saved-filters-preview text-muted small cursor-pointer" style="display:none;">
${__("Show Saved")}
</a>
</li>
</ul>
<div class="saved-filters" style="display:none;margin-top:4px;"></div>
</div>
</div>
`);
		this.$saved_filters_wrapper = this._$sidebar.find("#address-map-saved-filters");

		// Legend section (hidden until rules with labels are present)
		this._$sidebar.append(`
<div class="sidebar-section" id="address-map-legend-section" style="display:none;">
<div class="list-filters">
<div class="sidebar-label text-muted small mb-1">${__("Legend")}</div>
<div id="address-map-legend-items" style="display:flex;flex-direction:column;gap:4px;"></div>
</div>
</div>
`);
		this.$legend_section = this._$sidebar.find("#address-map-legend-section");
		this.$legend_items = this._$sidebar.find("#address-map-legend-items");

		this._setup_saved_filter_input();
		this._bind_saved_filters();
	}

	_setup_saved_filter_input() {
		const $input_area = this.$saved_filters_wrapper.find(".filter-input-area");
		this._filter_name_ctrl = frappe.ui.form.make_control({
			df: {
				fieldtype: "Data",
				placeholder: __("Save filters as\u2026"),
				input_class: "input-xs",
			},
			parent: $input_area,
			render_input: 1,
		});

		this._filter_name_ctrl.$input.keydown(
			frappe.utils.debounce((e) => {
				const value = this._filter_name_ctrl.get_value();
				if (e.which === frappe.ui.keyCode["ENTER"] && value) {
					this._save_filter(value).then(() => {
						this._filter_name_ctrl.set_value("");
						this._refresh_saved_filters();
					});
				}
			}, 300),
		);
	}

	_bind_saved_filters() {
		const $w = this.$saved_filters_wrapper;

		// Toggle saved list visibility
		$w.find(".saved-filters-preview").on("click", () => {
			const $saved = $w.find(".saved-filters");
			const show = $saved.is(":hidden");
			$saved.toggle(show);
			$w.find(".saved-filters-preview").text(show ? __("Hide Saved") : __("Show Saved"));
		});

		// Click a saved filter pill -> apply it
		$w.on("click", ".filter-pill .filter-name", (e) => {
			const $pill = $(e.currentTarget).closest(".filter-pill");
			$w.find(".filter-pill").removeClass("btn-primary-light").addClass("btn-default");
			$pill.removeClass("btn-default").addClass("btn-primary-light");
			try {
				const filters = JSON.parse($pill.attr("data-filters") || "[]");
				this._apply_saved_filter(filters);
			} catch (_) {
				// ignore JSON parse errors
			}
		});

		// Remove a saved filter pill
		$w.on("click", ".filter-pill .remove", (e) => {
			e.stopPropagation();
			const $pill = $(e.currentTarget).closest(".filter-pill");
			const name = $pill.attr("data-name");
			frappe.confirm(__("Remove this saved filter?"), () => {
				frappe.db.delete_doc("List Filter", name).then(() => this._refresh_saved_filters());
			});
		});
	}

	_save_filter(filter_name) {
		const doctype = this.$doctype_select.val();
		if (!doctype || !this.filter_group) return Promise.resolve();
		const filters = JSON.stringify(this.filter_group.get_filters());
		return frappe.db.insert({
			doctype: "List Filter",
			reference_doctype: doctype,
			filter_name,
			for_user: frappe.session.user,
			filters,
		});
	}

	_refresh_saved_filters() {
		const doctype = this.$doctype_select.val();
		if (!doctype) return;

		frappe.db
			.get_list("List Filter", {
				fields: ["name", "filter_name", "for_user", "filters"],
				filters: { reference_doctype: doctype },
				or_filters: [
					["for_user", "=", frappe.session.user],
					["for_user", "=", ""],
				],
				order_by: "filter_name asc",
			})
			.then((rows) => {
				const $saved = this.$saved_filters_wrapper.find(".saved-filters");
				$saved.find(".filter-pill").remove();
				const list = rows || [];
				list.forEach((row) => {
					$saved.append(`
<div class="list-link filter-pill list-sidebar-button btn btn-default"
data-name="${frappe.utils.escape_html(row.name)}"
data-filters="${frappe.utils.escape_html(row.filters || "[]")}">
<a class="ellipsis filter-name">${frappe.utils.escape_html(row.filter_name)}</a>
<a class="remove">${frappe.utils.icon("close")}</a>
</div>
`);
				});

				const $preview = this.$saved_filters_wrapper.find(".saved-filters-preview");
				list.length ? $preview.show() : $preview.hide();
			});
	}

	_apply_saved_filter(filters) {
		if (!this.filter_group) return;
		this.filter_group.clear_filters();
		if (filters && filters.length) {
			this.filter_group.add_filters_to_filter_group(filters);
		}
		this.filter_group.update_filter_button();
		this._on_filter_change();
	}

	_toggle_sidebar() {
		const show = JSON.parse(localStorage.show_sidebar || "true");
		localStorage.show_sidebar = !show;
		this._show_or_hide_sidebar();
	}

	_show_or_hide_sidebar() {
		const show = JSON.parse(localStorage.show_sidebar || "true");
		this.page.sidebar.toggle(show);
		// The main section uses `col` (auto-expanding), so hiding the sidebar
		// automatically lets Bootstrap fill the row — no class changes needed.
		if (this.map) {
			setTimeout(() => this.map.invalidateSize(), 200);
		}
	}

	// ──────────────────────────────────────────────────────────────
	// Filter area (page_form row, like list view)
	// ──────────────────────────────────────────────────────────────

	_setup_filter_area() {
		this.page.page_form.removeClass("row").addClass("flex");
		this.page.show_form();
		this.$filter_section = $('<div class="filter-section flex">').appendTo(this.page.page_form);
	}

	_setup_filter_group(doctype, initial_filters) {
		if (this.filter_group) {
			this.filter_group.wrapper && this.filter_group.wrapper.empty();
			this.filter_group = null;
		}

		// Re-build the filter selector for the new doctype
		this.$filter_section.find(".filter-selector").remove();

		const $filter_selector = $(`
<div class="filter-selector">
<div class="btn-group">
<button class="btn btn-default btn-sm filter-button">
<span class="filter-icon">${frappe.utils.icon("es-line-filter")}</span>
<span class="button-label hidden-xs">${__("Filter")}</span>
</button>
<button class="btn btn-default btn-sm filter-x-button" title="${__("Clear all filters")}">
<span class="filter-icon">${frappe.utils.icon("es-small-close")}</span>
</button>
</div>
</div>
`).appendTo(this.$filter_section);

		const $filter_button = $filter_selector.find(".filter-button");
		const $filter_x_button = $filter_selector.find(".filter-x-button");

		this.filter_group = new frappe.ui.FilterGroup({
			parent: this.$filter_section,
			doctype,
			filter_button: $filter_button,
			filter_x_button: $filter_x_button,
			default_filters: [],
			on_change: () => this._on_filter_change(),
		});

		// Frappe's set_clear_all_filters_event calls clear_filters() but omits on_change,
		// so attach our own listener after FilterGroup has bound its handler.
		$filter_x_button.on("click", () => this._on_filter_change());

		frappe.model.with_doctype(doctype, () => {
			if (initial_filters && initial_filters.length) {
				this.filter_group.add_filters_to_filter_group(initial_filters);
			}
			this.$saved_filters_wrapper.show();
			this._refresh_saved_filters();
			this._load_map_data();
		});
	}

	_on_filter_change() {
		clearTimeout(this._filter_change_timer);
		this._filter_change_timer = setTimeout(() => this._load_map_data(), 500);
	}

	// ──────────────────────────────────────────────────────────────
	// Map container (main section)
	// ──────────────────────────────────────────────────────────────

	_setup_map_container() {
		this.page.main.addClass("frappe-card");
		this.$map_wrapper = $(`
<div style="height:calc(100vh - 185px);width:100%;position:relative;">
<div id="address-map-container" style="height:100%;width:100%;"></div>
<div id="address-map-loading"
style="display:none;position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);
       background:rgba(255,255,255,.85);padding:12px 20px;border-radius:4px;">
${__("Loading\u2026")}
</div>
</div>
`);
		this.page.main.append(this.$map_wrapper);

		// Persistent event delegation — works regardless of when popups open/close
		this.$map_wrapper.on("click", ".address-map-assign-btn", (evt) => {
			evt.preventDefault();
			const $btn = $(evt.currentTarget);
			const doctype = $btn.data("doctype");
			const name = $btn.data("name");
			const assigned = parseInt($btn.data("assigned")) === 1;
			if (assigned) {
				frappe.call({
					method: "frappe.desk.form.assign_to.remove",
					args: { doctype, name, assign_to: frappe.session.user },
				}).then(() => {
					frappe.show_alert({ message: __("Unassigned"), indicator: "blue" });
					this._preserve_zoom = true;
					this._suppress_count = true;
					this._load_map_data();
				});
			} else {
				frappe.call({
					method: "address_map.api.assign_to_me",
					args: { doctype, name },
				}).then(() => {
					frappe.show_alert({ message: __("Assigned to you"), indicator: "green" });
					this._preserve_zoom = true;
					this._suppress_count = true;
					this._load_map_data();
				});
			}
		});
	}

	_init_map() {
		const defaults = frappe.utils.map_defaults;
		L.Icon.Default.imagePath = defaults.image_path;

		this.map = L.map("address-map-container").setView(defaults.center, defaults.zoom);
		L.tileLayer(defaults.tiles, defaults.options).addTo(this.map);
		L.control.scale().addTo(this.map);

		if (L.control.locate) {
			L.control.locate({ position: "topright" }).addTo(this.map);
		}

		this.marker_layer = null;
		this.pinned_layer = L.featureGroup().addTo(this.map);
		this.location_layer = L.featureGroup().addTo(this.map);

		this._load_pinned_addresses();
		this._locate_current_position();
	}

	_locate_current_position() {
		if (!navigator.geolocation) return;

		navigator.geolocation.getCurrentPosition(
			(pos) => {
				const lat = pos.coords.latitude;
				const lng = pos.coords.longitude;
				const accuracy = pos.coords.accuracy;
				const color = this._settings.location_color || "#4a90e2";
				const shape = (this._settings.location_shape || "circle").toLowerCase();

				this.location_layer.clearLayers();

				// Accuracy circle (always shown regardless of shape)
				L.circle([lat, lng], {
					radius: accuracy,
					color: color,
					fillColor: color,
					fillOpacity: 0.1,
					weight: 1,
				}).addTo(this.location_layer);

				// Position marker using the same helper as pinned markers
				this._make_pinned_marker([lat, lng], color, shape)
					.bindTooltip(__("You are here"), { permanent: false })
					.addTo(this.location_layer);

				if (!this.marker_layer) {
					this.map.setView([lat, lng], 13);
				}
			},
			() => {
				// Permission denied or unavailable — silently skip
			},
			{ timeout: 10000, maximumAge: 60000 },
		);
	}

	_load_pinned_addresses() {
		frappe
			.call({ method: "address_map.api.get_pinned_addresses" })
			.then((r) => this._render_pinned(r.message || {}));
	}

	_render_pinned(geojson) {
		this.pinned_layer.clearLayers();
		const features = geojson.features || [];
		features.forEach((feature) => {
			const [lng, lat] = feature.geometry.coordinates;
			const color = feature.properties.color || "#e74c3c";
			const shape = (feature.properties.shape || "Circle").toLowerCase();
			const marker = this._make_pinned_marker([lat, lng], color, shape);
			marker.bindPopup(feature.properties.popup, { maxWidth: 300, minWidth: 180 }).addTo(this.pinned_layer);
		});
		if (features.length && !this.marker_layer) {
			this.map.fitBounds(this.pinned_layer.getBounds(), { padding: [40, 40] });
		}
	}

	_make_pinned_marker(latlng, color, shape) {
		if (shape === "circle") {
			return L.circleMarker(latlng, {
				radius: 9,
				fillColor: color,
				color: "#fff",
				weight: 2,
				opacity: 1,
				fillOpacity: 0.9,
			});
		}
		if (shape === "pin") {
			const icon = L.divIcon({
				className: "",
				html: `<div style="filter:drop-shadow(0 1px 2px rgba(0,0,0,.4))">
<img src="${L.Icon.Default.imagePath}marker-icon.png"
     style="width:25px;height:41px;display:block;
            filter:sepia(1) saturate(5) hue-rotate(${this._color_to_hue(color)}deg)">
</div>`,
				iconSize: [25, 41],
				iconAnchor: [12, 41],
				popupAnchor: [1, -34],
			});
			return L.marker(latlng, { icon });
		}
		// Square and Diamond
		const rotate = shape === "diamond" ? "rotate(45deg)" : "none";
		const icon = L.divIcon({
			className: "",
			html: `<div style="
width:16px;height:16px;
background:${color};
border:2px solid #fff;
box-shadow:0 1px 3px rgba(0,0,0,.4);
transform:${rotate};
border-radius:2px;
"></div>`,
			iconSize: [16, 16],
			iconAnchor: [8, 8],
			popupAnchor: [0, -10],
		});
		return L.marker(latlng, { icon });
	}

	_color_to_hue(hex) {
		const r = parseInt(hex.slice(1, 3), 16) / 255;
		const g = parseInt(hex.slice(3, 5), 16) / 255;
		const b = parseInt(hex.slice(5, 7), 16) / 255;
		const max = Math.max(r, g, b),
			min = Math.min(r, g, b);
		let h = 0;
		if (max !== min) {
			const d = max - min;
			if (max === r) h = ((g - b) / d + (g < b ? 6 : 0)) / 6;
			else if (max === g) h = ((b - r) / d + 2) / 6;
			else h = ((r - g) / d + 4) / 6;
		}
		return Math.round(h * 360 - 210 + 360) % 360;
	}

	// ──────────────────────────────────────────────────────────────
	// DocType loading & selection
	// ──────────────────────────────────────────────────────────────

	_load_views() {
		frappe.call({ method: "address_map.api.get_views" }).then((r) => {
			if (!r.message || !r.message.length) {
				frappe.msgprint(__("No views configured in Address Map Settings."));
				return;
			}
			r.message.forEach((entry) => {
				this.$doctype_select.append(
					`<option value="${frappe.utils.escape_html(entry.doctype)}"
          data-via="${frappe.utils.escape_html(entry.via || "")}"
          data-via-field="${frappe.utils.escape_html(entry.via_field || "")}"
          data-display-name="${frappe.utils.escape_html(entry.display_name || "")}"
          data-allow-assign="${entry.allow_assign ? "1" : "0"}">
 ${frappe.utils.escape_html(entry.label)}
</option>`,
				);
			});

			// Pre-select: route_options (from list view button) > settings default
			const route_opts = frappe.route_options || {};
			frappe.route_options = null;
			const def = route_opts.doctype || (this._settings && this._settings.default_doctype);
			this._pending_filters = route_opts.filters || null;
			if (def) {
				// Match by label text first (handles display_name entries), then by doctype value
				let matched = false;
				this.$doctype_select.find("option").each((_, opt) => {
					if ($(opt).text().trim() === def || $(opt).val() === def) {
						$(opt).prop("selected", true);
						matched = true;
						return false;
					}
				});
				if (matched) this._on_doctype_change();
			}
		});
	}

	_on_doctype_change() {
		const selected = this.$doctype_select.find("option:selected");
		const doctype = selected.val();
		if (!doctype) return;

		const initial_filters = this._pending_filters
			? typeof this._pending_filters === "string"
				? JSON.parse(this._pending_filters)
				: this._pending_filters
			: null;
		this._pending_filters = null;

		this._setup_filter_group(doctype, initial_filters);
	}

	// ──────────────────────────────────────────────────────────────
	// Map data
	// ──────────────────────────────────────────────────────────────

	_load_map_data() {
		const selected = this.$doctype_select.find("option:selected");
		const doctype = selected.val();
		if (!doctype) return;

		const via = selected.data("via") || null;
		const via_field = selected.data("via-field") || null;
		const display_name = selected.data("display-name") || null;
		const allow_assign = selected.data("allow-assign") !== "0";
		const doctype_label = selected.text().trim();
		const filters = this.filter_group ? JSON.stringify(this.filter_group.get_filters()) : null;

		this._set_loading(true);
		this._render_legend([]);
		frappe
			.call({
				method: "address_map.api.get_map_data",
				args: { doctype, via, via_field, filters, display_name },
			})
			.then((r) => {
				this._set_loading(false);
				this._render_markers(r.message || {}, { preserve_zoom: this._preserve_zoom, suppress_count: this._suppress_count, doctype_label, allow_assign });
				this._preserve_zoom = false;
				this._suppress_count = false;
			})
			.catch(() => this._set_loading(false));
	}

	_render_legend(legend) {
		this.$legend_items.empty();
		if (!legend || !legend.length) {
			this.$legend_section.hide();
			return;
		}
		legend.forEach((item) => {
			const label = frappe.utils.escape_html(item.label);
			let iconHtml;
			if (item.hide) {
				iconHtml = `<span style="display:inline-flex;align-items:center;justify-content:center;width:14px;height:14px;color:var(--text-muted);font-size:13px;line-height:1;">&#8856;</span>`;
			} else {
				const color = item.color || "#3388ff";
				const shape = item.shape || "circle";
				if (shape === "circle") {
					iconHtml = `<span style="display:inline-block;width:12px;height:12px;border-radius:50%;background:${color};border:2px solid #fff;box-shadow:0 0 0 1px ${color};"></span>`;
				} else if (shape === "diamond") {
					iconHtml = `<span style="display:inline-block;width:10px;height:10px;background:${color};border:1.5px solid #fff;box-shadow:0 0 0 1px ${color};transform:rotate(45deg);"></span>`;
				} else if (shape === "pin") {
					iconHtml = `<span style="display:inline-block;font-size:14px;line-height:1;">📍</span>`;
				} else {
					// square
					iconHtml = `<span style="display:inline-block;width:12px;height:12px;background:${color};border:2px solid #fff;box-shadow:0 0 0 1px ${color};"></span>`;
				}
			}
			this.$legend_items.append(`<span style="display:inline-flex;align-items:center;gap:6px;font-size:0.85em;">${iconHtml}<span>${label}</span></span>`);
		});
		this.$legend_section.show();
	}

	_render_markers(geojson, { preserve_zoom = false, suppress_count = false, doctype_label = "", allow_assign = true } = {}) {
		const defaultColor = this._settings.default_marker_color || "#3388ff";
		const defaultShape = (this._settings.default_marker_shape || "circle").toLowerCase();
		const rulesLegend = geojson.legend || [];
		const defaultEntry = doctype_label
			? [{ label: doctype_label, color: defaultColor, shape: defaultShape, hide: false }]
			: [];
		this._render_legend([...defaultEntry, ...rulesLegend]);
		if (this.marker_layer) {
			this.map.removeLayer(this.marker_layer);
			this.marker_layer = null;
		}

		const features = geojson.features;
		if (!features || !features.length) {
			frappe.show_alert({
			message: __("No geocoded addresses found for this view."),
				indicator: "orange",
			});
			return;
		}

		this.marker_layer = L.featureGroup();

		features.forEach((feature) => {
			const [lng, lat] = feature.geometry.coordinates;
			const props = feature.properties;
			const color = props.color || defaultColor;
			const shape = props.shape || defaultShape;
			const assignLabel = props.is_mine ? __("Unassign") : __("Assign to me");
			const assignHtml = allow_assign
				? `<hr style="margin:4px 0"><a class="address-map-assign-btn" href="#"
				data-doctype="${frappe.utils.escape_html(props.doctype)}"
				data-name="${frappe.utils.escape_html(props.name)}"
				data-assigned="${props.is_mine ? 1 : 0}"
				style="font-size:.85em">${assignLabel}</a>`
				: "";
			const marker = this._make_pinned_marker([lat, lng], color, shape).bindPopup(props.popup + assignHtml, {
				maxWidth: 300,
				minWidth: 180,
			});
			// Remove Leaflet's built-in click-opens-popup so we can manage it ourselves
			marker.off("click");
			// Hover: open popup; delay close so moving into the popup doesn't dismiss it
			marker.on("mouseover", function () {
				clearTimeout(this._closeTimer);
				if (!this._am_pinned) this.openPopup();
			});
			marker.on("mouseout", function () {
				if (this._am_pinned) return;
				const self = this;
				this._closeTimer = setTimeout(function () {
					if (!self._am_pinned) self.closePopup();
				}, 100);
			});
			// Keep popup open while the mouse is inside it
			marker.on("popupopen", function () {
				const el = this.getPopup().getElement();
				if (!el) return;
				const self = this;
				el.addEventListener("mouseenter", function () {
					clearTimeout(self._closeTimer);
				});
				el.addEventListener("mouseleave", function () {
					if (self._am_pinned) return;
					self._closeTimer = setTimeout(function () {
						if (!self._am_pinned) self.closePopup();
					}, 100);
				});
			});
			// Click: toggle pinned state
			marker.on("click", function (e) {
				L.DomEvent.stopPropagation(e);
				if (this._am_pinned) {
					this._am_pinned = false;
					this.closePopup();
				} else {
					this._am_pinned = true;
					this.openPopup();
				}
			});
			// Closing the popup via the × button should also unpin
			marker.on("popupclose", function () {
				this._am_pinned = false;
			});
			this.marker_layer.addLayer(marker);
		});
		this.marker_layer.addTo(this.map);

		if (!preserve_zoom) {
			const combined = L.featureGroup([this.marker_layer, this.pinned_layer]);
			if (combined.getLayers().length) {
				this.map.fitBounds(combined.getBounds(), { padding: [40, 40] });
			}
		}

		if (!suppress_count) {
			frappe.show_alert({
				message: __("{0} address(es) shown", [features.length]),
				indicator: "green",
			});
		}
	}

	_set_loading(state) {
		this.$map_wrapper.find("#address-map-loading").toggle(state);
	}

	refresh() {
		if (this.map) {
			setTimeout(() => this.map.invalidateSize(), 100);
		}
		this._load_pinned_addresses();
		this._locate_current_position();

		// Consume route_options set by the list-view "Address Map" button.
		// on_page_show fires (not on_page_load) when the page is already in DOM.
		const route_opts = frappe.route_options || {};
		const incoming_doctype = route_opts.doctype || null;
		const incoming_filters = route_opts.filters || null;

		if (incoming_doctype || incoming_filters) {
			frappe.route_options = null;
			const current_doctype = this.$doctype_select.val();

			if (incoming_doctype && incoming_doctype !== current_doctype) {
				// Different doctype: switch selection; filters applied via _pending_filters
				this._pending_filters = incoming_filters;
				this.$doctype_select.val(incoming_doctype);
				if (this.$doctype_select.val()) {
					this._on_doctype_change();
				}
			} else if (incoming_filters && this.filter_group) {
				// Same doctype, just replace active filters
				const filters = typeof incoming_filters === "string" ? JSON.parse(incoming_filters) : incoming_filters;
				this._apply_saved_filter(filters);
			}
		} else if (this.$doctype_select.val()) {
			// No route_options: re-fetch map data for the currently selected doctype
			this._load_map_data();
		}

		// Always re-fetch saved filters so the list is current after navigating away and back
		this._refresh_saved_filters();
	}
}
