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
		this._views = [];
		this._view_layers = new Map(); // idx -> L.featureGroup
		this._view_legends = new Map(); // idx -> legend[]
		this._filter_group_idx = null;

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
<div class="sidebar-label text-muted small mb-1">${__("Views")}</div>
<div id="address-map-view-list" style="display:flex;flex-direction:column;gap:4px;"></div>
</div>
</div>
`);
		this.$view_list = this._$sidebar.find("#address-map-view-list");

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
		const doctype = this._filter_group_idx !== null && this._views[this._filter_group_idx]
			? this._views[this._filter_group_idx].doctype
			: null;
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
		const doctype = this._filter_group_idx !== null && this._views[this._filter_group_idx]
			? this._views[this._filter_group_idx].doctype
			: null;
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
			let applied_filters = false;
			if (initial_filters && initial_filters.length) {
				this.filter_group.add_filters_to_filter_group(initial_filters);
				applied_filters = true;
			}
			this.$saved_filters_wrapper.show();
			this._refresh_saved_filters();
			// Only reload when initial filters were applied — the caller (_on_view_toggle)
			// already triggered a load without filters; reloading here would duplicate it.
			if (applied_filters && this._filter_group_idx !== null) {
				this._load_view_layer(this._filter_group_idx);
			}
		});
	}

	_on_filter_change() {
		clearTimeout(this._filter_change_timer);
		this._filter_change_timer = setTimeout(() => {
			if (this._filter_group_idx !== null) {
				this._load_view_layer(this._filter_group_idx);
			}
		}, 500);
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

				if (!this._view_layers.size) {
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
			marker.bindPopup(feature.properties.popup, { maxWidth: 300, minWidth: 180 });
			this._bind_popup_interactions(marker);
			marker.addTo(this.pinned_layer);
		});
		if (features.length && !this._view_layers.size) {
			this.map.fitBounds(this.pinned_layer.getBounds(), { padding: [40, 40] });
		}
	}

	// Attach hover-to-open, click-to-pin, and assign-button behaviour to a marker.
	_bind_popup_interactions(marker) {
		const page = this;
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
		// Keep popup open while the mouse is inside it; also wire assign button
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
			// Leaflet blocks click propagation on popup containers, so we
			// bind the assign button directly on the popup element here.
			const btn = el.querySelector(".address-map-assign-btn");
			if (btn) {
				btn.addEventListener("click", function (evt) {
					evt.preventDefault();
					const doctype = btn.dataset.doctype;
					const name = btn.dataset.name;
					const assigned = parseInt(btn.dataset.assigned) === 1;
					if (assigned) {
						frappe.call({
							method: "frappe.desk.form.assign_to.remove",
							args: { doctype, name, assign_to: frappe.session.user },
						}).then(() => {
							frappe.show_alert({ message: __("Unassigned"), indicator: "blue" });
							page._preserve_zoom = true;
							page._suppress_count = true;
							page._reload_all_active_views();
						});
					} else {
						frappe.call({
							method: "address_map.api.assign_to_me",
							args: { doctype, name },
						}).then(() => {
							frappe.show_alert({ message: __("Assigned to you"), indicator: "green" });
							page._preserve_zoom = true;
							page._suppress_count = true;
							page._reload_all_active_views();
						});
					}
				});
			}
			// Copy address to clipboard when the address link is clicked
			const copyLinks = el.querySelectorAll(".address-map-copy-address");
			copyLinks.forEach((link) => {
				link.addEventListener("click", function (evt) {
					evt.preventDefault();
					const text = link.dataset.address || "";
					if (!text) return;
					(navigator.clipboard
						? navigator.clipboard.writeText(text)
						: Promise.reject()
					).catch(() => frappe.utils.copy_to_clipboard(text))
					.then(() => {
						frappe.show_alert({ message: __("Address copied to clipboard"), indicator: "green" });
					});
				});
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
		// Square and Diamond
		if (shape === "triangle") {
			const icon = L.divIcon({
				className: "",
				html: `<svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 18 18">
<polygon points="9,1 17,17 1,17" fill="${color}" stroke="#fff" stroke-width="2" stroke-linejoin="round"/>
</svg>`,
				iconSize: [18, 18],
				iconAnchor: [9, 9],
				popupAnchor: [0, -11],
			});
			return L.marker(latlng, { icon });
		}
		if (shape === "star") {
			const icon = L.divIcon({
				className: "",
				html: `<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 20 20">
<polygon points="10,1 12.9,7 19.5,7.6 14.5,12 16.2,18.5 10,15 3.8,18.5 5.5,12 0.5,7.6 7.1,7" fill="${color}" stroke="#fff" stroke-width="1.5" stroke-linejoin="round"/>
</svg>`,
				iconSize: [20, 20],
				iconAnchor: [10, 10],
				popupAnchor: [0, -12],
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

	// ──────────────────────────────────────────────────────────────
	// DocType loading & selection
	// ──────────────────────────────────────────────────────────────

	_make_view_checkbox(i, label) {
		const $item = $(`<div data-idx="${i}" style="display:flex;align-items:center;gap:6px;cursor:pointer;padding:2px 0;user-select:none;">
<span class="address-map-view-check" style="display:inline-flex;align-items:center;justify-content:center;width:14px;height:14px;min-width:14px;border:1px solid var(--gray-500);border-radius:4px;background:transparent;transition:background .15s,border-color .15s;"></span>
<span style="font-size:var(--text-sm);">${frappe.utils.escape_html(label)}</span>
</div>`);
		$item.on("click", () => {
			const checked = $item.data("checked") !== true;
			$item.data("checked", checked);
			this._set_view_checkbox_visual($item, checked);
			this._on_view_toggle(i, checked);
		});
		return $item;
	}

	_set_view_checkbox_visual($item, checked) {
		const $box = $item.find(".address-map-view-check");
		if (checked) {
			$box.css({ background: "var(--primary)", borderColor: "var(--primary)" });
			$box.html(`<svg viewBox="0 0 8 7" fill="none" xmlns="http://www.w3.org/2000/svg" style="width:9px;height:9px;"><path d="M1 4L2.667 5.8L7 1.2" stroke="white" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>`);
		} else {
			$box.css({ background: "", borderColor: "" });
			$box.html("");
		}
	}

	_load_views() {
		frappe.call({ method: "address_map.api.get_views" }).then((r) => {
			if (!r.message || !r.message.length) {
				frappe.msgprint(__("No views configured in Address Map Settings."));
				return;
			}
			this._views = r.message;
			r.message.forEach((entry, i) => {
				const $item = this._make_view_checkbox(i, entry.label);
				this.$view_list.append($item);
			});

			// Pre-select: route_options (from list view button) > settings default
			const route_opts = frappe.route_options || {};
			frappe.route_options = null;
			const raw_def = route_opts.doctype || (this._settings && this._settings.default_doctype);
			this._pending_filters = route_opts.filters || null;
			if (raw_def) {
				const defs = String(raw_def).split(",").map((s) => s.trim()).filter(Boolean);
				this.$view_list.find("div[data-idx]").each((_, el) => {
					const $item = $(el);
					const idx = parseInt($item.data("idx"));
					const view = this._views[idx];
					if (view && defs.some((d) => d === view.label || d === view.display_name || d === view.doctype)) {
						$item.data("checked", true);
						this._set_view_checkbox_visual($item, true);
						this._on_view_toggle(idx, true);
					}
				});
			}
		});
	}

	_on_view_toggle(idx, checked) {
		if (checked) {
			this._load_view_layer(idx);
		} else {
			if (this._view_layers.has(idx)) {
				this.map.removeLayer(this._view_layers.get(idx));
				this._view_layers.delete(idx);
			}
			this._view_legends.delete(idx);
			this._combine_and_render_legend();
		}
		this._update_filter_section();
	}

	_get_checked_view_indices() {
		const indices = [];
		this.$view_list.find("div[data-idx]").each((_, el) => {
			if ($(el).data("checked") === true) indices.push(parseInt($(el).data("idx")));
		});
		return indices;
	}

	_update_filter_section() {
		const active = this._get_checked_view_indices();
		if (active.length === 1) {
			const idx = active[0];
			const view = this._views[idx];
			if (this._filter_group_idx !== idx) {
				this._filter_group_idx = idx;
				const initial_filters = this._pending_filters
					? typeof this._pending_filters === "string"
						? JSON.parse(this._pending_filters)
						: this._pending_filters
					: null;
				this._pending_filters = null;
				this._setup_filter_group(view.doctype, initial_filters);
			}
		} else {
			if (this.filter_group) {
				this.filter_group.wrapper && this.filter_group.wrapper.empty();
				this.filter_group = null;
			}
			this.$filter_section.find(".filter-selector").remove();
			this._filter_group_idx = null;
			this.$saved_filters_wrapper.hide();
		}
	}

	// ──────────────────────────────────────────────────────────────
	// Map data
	// ──────────────────────────────────────────────────────────────

	_load_view_layer(idx) {
		const view = this._views[idx];
		if (!view) return;

		const filters = (this._filter_group_idx === idx && this.filter_group)
			? JSON.stringify(this.filter_group.get_filters())
			: null;

		this._set_loading(true);
		frappe
			.call({
				method: "address_map.api.get_map_data",
				args: {
					doctype: view.doctype,
					via: view.via || null,
					via_field: view.via_field || null,
					filters,
					view_name: view.name || null,
					display_name: view.display_name || null,
				},
			})
			.then((r) => {
				this._set_loading(false);
				const geojson = r.message || {};
				const allow_assign = view.allow_assign !== false;
				const doctype_label = view.label || view.doctype;
				const globalDefaultColor = this._settings.default_marker_color || "#3388ff";
				const globalDefaultShape = (this._settings.default_marker_shape || "circle").toLowerCase();
				const viewDefaultColor = view.default_marker_color || globalDefaultColor;
				const viewDefaultShape = (view.default_marker_shape || globalDefaultShape).toLowerCase();

				if (this._view_layers.has(idx)) {
					this.map.removeLayer(this._view_layers.get(idx));
				}
				const layer = this._build_marker_layer(geojson, { allow_assign, defaultColor: viewDefaultColor, defaultShape: viewDefaultShape });
				this._view_layers.set(idx, layer);
				if (this.map) layer.addTo(this.map);

				this._view_legends.set(idx, [
					{ label: doctype_label, color: viewDefaultColor, shape: viewDefaultShape, hide: false },
					...(geojson.legend || []),
				]);
				this._combine_and_render_legend();

				if (!this._preserve_zoom) {
					this._fit_active_bounds();
				}
				this._preserve_zoom = false;

				const features = geojson.features || [];
				if (!features.length) {
					frappe.show_alert({ message: __("No geocoded addresses found for this view."), indicator: "orange" });
				} else if (!this._suppress_count) {
					frappe.show_alert({ message: __("{0} address(es) shown", [features.length]), indicator: "green" });
				}
				this._suppress_count = false;
			})
			.catch(() => this._set_loading(false));
	}

	_combine_and_render_legend() {
		const combined = [];
		this._get_checked_view_indices().forEach((idx) => {
			if (this._view_legends.has(idx)) combined.push(...this._view_legends.get(idx));
		});
		this._render_legend(combined);
	}

	_fit_active_bounds() {
		const layers = [...this._view_layers.values()];
		if (this.pinned_layer) layers.push(this.pinned_layer);
		if (this.location_layer) layers.push(this.location_layer);
		const groups = layers.filter((l) => l.getLayers && l.getLayers().length > 0);
		if (!groups.length) return;
		const combined = L.featureGroup(groups);
		this.map.fitBounds(combined.getBounds(), { padding: [40, 40] });
	}

	_reload_all_active_views() {
		this._get_checked_view_indices().forEach((idx) => this._load_view_layer(idx));
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
				} else if (shape === "square") {
					iconHtml = `<span style="display:inline-block;width:12px;height:12px;background:${color};border:2px solid #fff;box-shadow:0 0 0 1px ${color};border-radius:2px;"></span>`;
				} else if (shape === "diamond") {
					iconHtml = `<span style="display:inline-block;width:10px;height:10px;background:${color};border:1.5px solid #fff;box-shadow:0 0 0 1px ${color};transform:rotate(45deg);"></span>`;
				} else if (shape === "triangle") {
					iconHtml = `<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 18 18" style="display:inline-block;vertical-align:middle;"><polygon points="9,1 17,17 1,17" fill="${color}" stroke="#fff" stroke-width="2" stroke-linejoin="round"/></svg>`;
				} else if (shape === "star") {
					iconHtml = `<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 20 20" style="display:inline-block;vertical-align:middle;"><polygon points="10,1 12.9,7 19.5,7.6 14.5,12 16.2,18.5 10,15 3.8,18.5 5.5,12 0.5,7.6 7.1,7" fill="${color}" stroke="#fff" stroke-width="1.5" stroke-linejoin="round"/></svg>`;
				} else {
					iconHtml = `<span style="display:inline-block;width:12px;height:12px;background:${color};border:2px solid #fff;box-shadow:0 0 0 1px ${color};border-radius:2px;"></span>`;
				}
			}
			this.$legend_items.append(`<span style="display:inline-flex;align-items:center;gap:6px;font-size:0.85em;">${iconHtml}<span>${label}</span></span>`);
		});
		this.$legend_section.show();
	}

	_build_marker_layer(geojson, { allow_assign = true, defaultColor = null, defaultShape = null } = {}) {
		defaultColor = defaultColor || this._settings.default_marker_color || "#3388ff";
		defaultShape = (defaultShape || this._settings.default_marker_shape || "circle").toLowerCase();
		const layer = L.featureGroup();
		const page = this;

		const features = geojson.features || [];
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
			this._bind_popup_interactions(marker);
			layer.addLayer(marker);
		});
		return layer;
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
			if (incoming_doctype) {
				// Find and check the matching view checkbox
				this.$view_list.find("div[data-idx]").each((_, el) => {
					const $item = $(el);
					const idx = parseInt($item.data("idx"));
					const view = this._views[idx];
						if (view && (view.label === incoming_doctype || view.display_name === incoming_doctype || view.doctype === incoming_doctype)) {
						if (!$item.data("checked")) {
							this._pending_filters = incoming_filters;
							$item.data("checked", true);
							this._set_view_checkbox_visual($item, true);
							this._on_view_toggle(idx, true);
						} else if (incoming_filters && this.filter_group) {
							const filters = typeof incoming_filters === "string"
								? JSON.parse(incoming_filters)
								: incoming_filters;
							this._apply_saved_filter(filters);
						}
						return false;
					}
				});
			} else if (incoming_filters && this.filter_group) {
				const filters = typeof incoming_filters === "string"
					? JSON.parse(incoming_filters)
					: incoming_filters;
				this._apply_saved_filter(filters);
			}
		} else {
			// No route_options: re-fetch data for all currently checked views
			this._reload_all_active_views();
		}

		// Always re-fetch saved filters so the list is current after navigating away and back
		this._refresh_saved_filters();
	}
}
