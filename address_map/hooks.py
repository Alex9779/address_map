app_name = "address_map"
app_title = "Address Map"
app_publisher = "ALITECS Alexander Leisentritt"
app_description = "Show documents with linked addresses on an interactive map"
app_icon = "octicon octicon-location"
app_color = "#3b82f6"
app_email = "info@alitecs.de"
app_license = "GPLv3"

after_install = "address_map.install.after_install"
before_uninstall = "address_map.install.before_uninstall"

doctype_js = {
	"Address": "public/js/address.js",
}

doc_events = {
	"Address": {
		"on_update": "address_map.geocoding.on_address_update",
	},
}
