import copy
import importlib.util
from pathlib import Path
import unittest


SPEC = importlib.util.spec_from_file_location(
    "indexed_verify", Path(__file__).resolve().parents[1] / "scripts" / "indexed_verify.py")
verify = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verify)


class VerificationTests(unittest.TestCase):
    def test_service_default_properties_are_allowed(self):
        actual = copy.deepcopy(verify.schemas())
        actual["index"]["@odata.etag"] = "service-version"
        actual["knowledgeBase"]["knowledgeSources"][0]["enableFreshness"] = None
        verify.matches(actual, verify.schemas())

    def test_acl_configuration_drift_is_rejected(self):
        for field, value in (("permissionFilter", None), ("filterable", False),
                             ("retrievable", True)):
            actual = copy.deepcopy(verify.schemas())
            acl = next(item for item in actual["index"]["fields"] if item["name"] == "groupIds")
            acl[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                verify.matches(actual, verify.schemas())

    def test_extra_knowledge_source_is_rejected(self):
        actual = copy.deepcopy(verify.schemas())
        actual["knowledgeBase"]["knowledgeSources"].append({"name": "unexpected"})
        with self.assertRaises(ValueError):
            verify.matches(actual, verify.schemas())

    def test_type_coercion_is_not_accepted(self):
        with self.assertRaises(ValueError):
            verify.matches({"filterable": 1}, {"filterable": True})


if __name__ == "__main__":
    unittest.main()
