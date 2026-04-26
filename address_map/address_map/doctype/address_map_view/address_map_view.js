// Copyright (c) 2026, ALITECS Alexander Leisentritt and contributors
// License: GPLv3

frappe.ui.form.on("Address Map View", {
	refresh(frm) {
		frm.page.$title_area.find(".title-text").off("click");
		frm.page.$title_area.find(".title-text").css("cursor", "default");
	},
});
