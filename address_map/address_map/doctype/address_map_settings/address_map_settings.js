// Copyright (c) 2026, ALITECS Alexander Leisentritt and contributors
// License: GPLv3

frappe.ui.form.on("Address Map Settings", {
	geocode_all(frm) {
		frappe.confirm(
			__(
				"This will geocode all addresses that do not have coordinates yet. " +
					"The job runs in the background and may take several minutes. Continue?"
			),
			() => {
				frappe
					.call({ method: "address_map.api.geocode_all_addresses" })
					.then((r) => {
						const count = r.message?.queued ?? "?";
						frappe.show_alert({
							message: __("{0} address(es) queued for geocoding.", [count]),
							indicator: "green",
						});
					});
			}
		);
	},
});
