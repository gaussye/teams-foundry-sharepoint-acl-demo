import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from native_site_field_repair import validate_metadata, FIELD
from native_indexer_experiment import LIBRARIES, SITE


class MetadataRepairTests(unittest.TestCase):
    def test_merge_changes_only_site_field_for_exact_files(self):
        rows = [{"id": str(i), "metadata_spo_item_name": lib[3]}
                for i, lib in enumerate(LIBRARIES)]
        payload = validate_metadata({"@odata.count": 3, "value": rows})
        self.assertTrue(all(set(r) == {"@search.action", "id", FIELD} and r[FIELD] == SITE
                            for r in payload))

    def test_unexpected_file_or_duplicate_keys_rejected(self):
        rows = [{"id": str(i), "metadata_spo_item_name": lib[3]}
                for i, lib in enumerate(LIBRARIES)]
        for bad in [rows[:-1], [{**r, "id": "same"} for r in rows],
                    [{**r, "metadata_spo_item_name": "business.docx"} for r in rows]]:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                validate_metadata({"@odata.count": 3, "value": bad})
