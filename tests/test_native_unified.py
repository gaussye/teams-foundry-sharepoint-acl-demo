from copy import deepcopy
from io import BytesIO
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from native_unified import (
    CHART_KS, DS, EXPECTED_FILES, INDEX, INDEXER, KB, KS, TEXT_KS,
    configuration, cutover, kb_replacement, validate_content, verify_definition,
)


class UnifiedNativeTests(unittest.TestCase):
    def test_one_source_indexer_all_three_libraries_and_formats_without_broader_auth(self):
        with patch("native_chart_live.request") as network:
            config = configuration()
            network.assert_not_called()
        source = config["datasource"]
        self.assertEqual(source["name"], DS)
        self.assertEqual(source["type"], "sharepoint")
        selections = source["container"]["query"].split(";")
        self.assertEqual(len(selections), 3)
        self.assertTrue(all(x.startswith("includeLibrary=https://") for x in selections))
        self.assertEqual({x.rsplit("/", 1)[1] for x in selections}, set(EXPECTED_FILES))
        self.assertNotIn("allSite", str(source))
        indexer = config["indexer"]
        self.assertEqual(indexer["name"], INDEXER)
        self.assertEqual(indexer["dataSourceName"], DS)
        self.assertEqual(indexer["targetIndexName"], INDEX)
        self.assertEqual(indexer["parameters"]["configuration"]["indexedFileNameExtensions"],
                         ".docx,.pdf,.png")
        self.assertIsNone(indexer["schedule"])
        self.assertTrue(indexer["disabled"])

    def test_merge_preserves_docx_text_and_appends_image_descriptions_before_index_output(self):
        config = configuration()
        skills = config["skillset"]["skills"]
        self.assertEqual(len(skills), 2)
        self.assertEqual(skills[0]["context"], "/document/normalized_images/*")
        self.assertEqual(skills[1]["@odata.type"], "#Microsoft.Skills.Text.MergeSkill")
        inputs = {x["name"]: x["source"] for x in skills[1]["inputs"]}
        self.assertEqual(inputs["text"], "/document/content")
        self.assertEqual(inputs["itemsToInsert"], "/document/normalized_images/*/description")
        self.assertEqual(inputs["offsets"], "/document/normalized_images/*/contentOffset")
        self.assertIn("AI-generated", skills[1]["insertPreTag"])
        self.assertNotIn("indexProjections", config["skillset"])
        self.assertNotIn("knowledgeStore", config["skillset"])
        self.assertEqual(config["indexer"]["outputFieldMappings"], [
            {"sourceFieldName": "/document/unifiedContent", "targetFieldName": "content"}])

    def test_single_file_record_maps_source_acl_and_site_from_source_not_expected_answers(self):
        config = configuration()
        fields = {f["name"]: f for f in config["index"]["fields"]}
        mappings = {x["sourceFieldName"]: x["targetFieldName"]
                    for x in config["indexer"]["fieldMappings"]}
        self.assertEqual(config["index"]["permissionFilterOption"], "enabled")
        for name in ("userIds", "groupIds"):
            self.assertEqual(fields[name]["permissionFilter"], name)
            self.assertFalse(fields[name]["retrievable"])
            self.assertEqual(mappings["metadata_" + ("user_ids" if name == "userIds" else "group_ids")],
                             name)
        for name in ("SharePointSiteUrl", "metadata_spo_item_path"):
            self.assertFalse(fields[name]["retrievable"])
        self.assertTrue(fields["SharePointSiteUrl"]["sharepointSiteUrl"])
        self.assertEqual(mappings["metadata_spo_site_url"], "SharePointSiteUrl")
        self.assertNotIn("parentId", fields)
        self.assertNotIn("metadata_user_ids", str(config["skillset"]))
        self.assertNotIn("expected.json", str(config))

    def test_kb_cutover_only_replaces_known_two_sources_preserving_other_settings_and_etag(self):
        baseline = {
            "name": KB, "models": [], "outputMode": "extractiveData",
            "retrievalReasoningEffort": {"kind": "minimal"},
            "knowledgeSources": [{"name": TEXT_KS}, {"name": CHART_KS}],
            "description": "keep", "@odata.etag": '"etag"',
        }
        body, etag = kb_replacement(baseline)
        self.assertEqual(body["knowledgeSources"], [{"name": KS}])
        self.assertEqual(body["description"], "keep")
        self.assertEqual(etag, '"etag"')
        self.assertEqual(len(baseline["knowledgeSources"]), 2)
        for change in ({"name": "remote"}, {"knowledgeSources": [{"name": TEXT_KS}]},
                       {"@odata.etag": None}, {"models": [{"model": "other"}]}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                kb_replacement({**deepcopy(baseline), **change})

    def test_datasource_readback_redacts_connection_but_not_scope_or_permissions(self):
        desired = configuration()["datasource"]
        actual = deepcopy(desired)
        actual["credentials"]["connectionString"] = None
        verify_definition(actual, desired, "datasource")
        self.assertIsNotNone(desired["credentials"]["connectionString"])
        actual["container"]["query"] = "includeLibrariesInSite=https://other"
        with self.assertRaises(ValueError):
            verify_definition(actual, desired, "datasource")

    def content_result(self):
        root = Path(__file__).resolve().parents[1] / "tests" / "fixtures"
        result = {"@odata.count": 7, "value": []}
        for item in json.loads((root / "sharepoint-originals" / "manifest.json").read_text(
                encoding="utf-8"))["files"]:
            result["value"].append({"metadata_spo_item_name": item["name"],
                                    "content": item["verificationCode"] + " " + str(item["allocation"])})
        for item in json.loads((root / "native-charts" / "expected.json").read_text(
                encoding="utf-8"))["files"]:
            result["value"].append({
                "metadata_spo_item_name": item["name"],
                "content": "[AI-generated image description] " + item["marker"] + " North Central South"})
        return result

    def test_normal_admin_validation_checks_all_content_without_elevation_or_acl_projection(self):
        journal = Mock(value={})
        with patch("native_unified.cli", return_value={"accessToken": "test-not-a-real-token"}), \
                patch("native_unified.urlopen",
                      return_value=BytesIO(json.dumps(self.content_result()).encode())) as network:
            validate_content(journal)
        operation = network.call_args.args[0]
        self.assertNotIn("elevated", str(operation.headers).lower())
        self.assertIn("X-ms-query-source-authorization", operation.headers)
        query = json.loads(operation.data)
        self.assertEqual(query["select"], "metadata_spo_item_name,content,sourceUrl")
        self.assertNotIn("semantic", query)
        self.assertTrue(journal.value["originalTextAndFourImageDescriptionsVerified"])
        self.assertNotIn("test-not-a-real-token", str(journal.value))

    def test_normal_validation_rejects_lost_docx_text_without_retrieval_retry(self):
        result = self.content_result()
        result["value"][0]["content"] = "description only, lost original text"
        with patch("native_unified.cli", return_value={"accessToken": "test-not-a-real-token"}), \
                patch("native_unified.urlopen",
                      return_value=BytesIO(json.dumps(result).encode())) as network:
            with self.assertRaisesRegex(ValueError, "DOCX content lost"):
                validate_content(Mock(value={}))
            network.assert_called_once()

    def test_cutover_requires_completed_non_elevated_content_validation_before_any_network(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "native-unified-validate-content-20261125.json"
            with patch("native_unified.EVIDENCE", Path(directory)), \
                    patch("native_unified.search") as network:
                with self.assertRaisesRegex(ValueError, "Content validation required"):
                    cutover(None)
                for evidence in (
                    {"status": "failed-no-automatic-retry"},
                    {"status": "success", "originalTextAndFourImageDescriptionsVerified": False,
                     "elevatedRead": False},
                    {"status": "success", "originalTextAndFourImageDescriptionsVerified": True,
                     "elevatedRead": True},
                ):
                    path.write_text(json.dumps(evidence), encoding="utf-8")
                    with self.assertRaisesRegex(ValueError, "validation incomplete"):
                        cutover(None)
                network.assert_not_called()


if __name__ == "__main__":
    unittest.main()
