# Copyright (c) 2026, ALITECS Alexander Leisentritt and contributors
# License: GPLv3


def get_custom_fields() -> dict:
	return {
		"Address": [
			{
				"fieldtype": "Section Break",
				"fieldname": "address_map_section",
				"label": "Geocoordinates",
				"insert_after": "links",
				"collapsible": 1,
			},
			{
				"fieldtype": "Float",
				"fieldname": "latitude",
				"label": "Latitude",
				"insert_after": "address_map_section",
				"precision": "8",
				"translatable": 0,
				"read_only": 0,
			},
			{
				"fieldtype": "Column Break",
				"fieldname": "address_map_col_break",
				"insert_after": "latitude",
			},
			{
				"fieldtype": "Float",
				"fieldname": "longitude",
				"label": "Longitude",
				"insert_after": "address_map_col_break",
				"precision": "8",
				"translatable": 0,
				"read_only": 0,
			},
			{
				"fieldtype": "Column Break",
				"fieldname": "address_map_col_break_2",
				"insert_after": "longitude",
			},
			{
				"fieldtype": "Geolocation",
				"fieldname": "geolocation",
				"label": "Geolocation",
				"insert_after": "address_map_col_break_2",
				"read_only": 1,
				"translatable": 0,
			},
		]
	}
