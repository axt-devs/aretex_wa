"""
aretex_wa/tests/test_basic.py
Basic tests to satisfy CI requirements.
"""

import frappe
import unittest


class TestWhatsappHandler(unittest.TestCase):

	def test_compute_priority_high_full_system(self):
		from aretex_wa.whatsapp_handler import compute_priority
		result = compute_priority("full_system", "hvac", "issue")
		self.assertEqual(result, "HIGH")

	def test_compute_priority_high_specific_area_hvac(self):
		from aretex_wa.whatsapp_handler import compute_priority
		result = compute_priority("specific_area", "hvac", "issue")
		self.assertEqual(result, "HIGH")

	def test_compute_priority_medium_specific_area_lighting(self):
		from aretex_wa.whatsapp_handler import compute_priority
		result = compute_priority("specific_area", "lighting", "issue")
		self.assertEqual(result, "MEDIUM")

	def test_compute_priority_low_query(self):
		from aretex_wa.whatsapp_handler import compute_priority
		result = compute_priority("full_system", "hvac", "query")
		self.assertEqual(result, "LOW")

	def test_decide_resource_engineer_full_system(self):
		from aretex_wa.whatsapp_handler import decide_resource_type
		result = decide_resource_type("full_system", "lighting")
		self.assertEqual(result, "Engineer")

	def test_decide_resource_engineer_hvac(self):
		from aretex_wa.whatsapp_handler import decide_resource_type
		result = decide_resource_type("specific_area", "hvac")
		self.assertEqual(result, "Engineer")

	def test_decide_resource_technician_lighting(self):
		from aretex_wa.whatsapp_handler import decide_resource_type
		result = decide_resource_type("specific_area", "lighting")
		self.assertEqual(result, "Technician")

	def test_decide_resource_technician_av(self):
		from aretex_wa.whatsapp_handler import decide_resource_type
		result = decide_resource_type("specific_device", "av")
		self.assertEqual(result, "Technician")
