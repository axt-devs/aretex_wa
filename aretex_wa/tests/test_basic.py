"""
aretex_wa/tests/test_basic.py
Unit tests for core business logic — no DB or API calls needed.
"""

import unittest


class TestComputePriority(unittest.TestCase):

	def test_full_system_is_high(self):
		from aretex_wa.whatsapp_handler import compute_priority
		self.assertEqual(compute_priority("full_system", "hvac", "issue"), "HIGH")

	def test_specific_area_hvac_is_high(self):
		from aretex_wa.whatsapp_handler import compute_priority
		self.assertEqual(compute_priority("specific_area", "hvac", "issue"), "HIGH")

	def test_specific_area_security_is_high(self):
		from aretex_wa.whatsapp_handler import compute_priority
		self.assertEqual(compute_priority("specific_area", "security", "issue"), "HIGH")

	def test_specific_area_lighting_is_medium(self):
		from aretex_wa.whatsapp_handler import compute_priority
		self.assertEqual(compute_priority("specific_area", "lighting", "issue"), "MEDIUM")

	def test_specific_device_is_medium(self):
		from aretex_wa.whatsapp_handler import compute_priority
		self.assertEqual(compute_priority("specific_device", "hvac", "issue"), "MEDIUM")

	def test_query_is_low(self):
		from aretex_wa.whatsapp_handler import compute_priority
		self.assertEqual(compute_priority("full_system", "hvac", "query"), "LOW")

	def test_disclosed_is_low(self):
		from aretex_wa.whatsapp_handler import compute_priority
		self.assertEqual(compute_priority("specific_area", "hvac", "disclosed"), "LOW")


class TestDecideResourceType(unittest.TestCase):

	def test_full_system_is_engineer(self):
		from aretex_wa.whatsapp_handler import decide_resource_type
		self.assertEqual(decide_resource_type("full_system", "lighting"), "Engineer")

	def test_hvac_is_engineer(self):
		from aretex_wa.whatsapp_handler import decide_resource_type
		self.assertEqual(decide_resource_type("specific_area", "hvac"), "Engineer")

	def test_security_is_engineer(self):
		from aretex_wa.whatsapp_handler import decide_resource_type
		self.assertEqual(decide_resource_type("specific_area", "security"), "Engineer")

	def test_lighting_is_technician(self):
		from aretex_wa.whatsapp_handler import decide_resource_type
		self.assertEqual(decide_resource_type("specific_area", "lighting"), "Technician")

	def test_av_is_technician(self):
		from aretex_wa.whatsapp_handler import decide_resource_type
		self.assertEqual(decide_resource_type("specific_device", "av"), "Technician")
