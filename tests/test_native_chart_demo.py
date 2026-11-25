from hashlib import sha256
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from native_chart_demo import (
    CASES, FOLDER, IMAGE_CONTEXT, INDEX, KS, LIBRARIES, MODEL_URI, NATIVE_KB,
    NATIVE_KS, configuration, knowledge_base_update, prepare_fixtures,
)


class NativeChartConfigurationTests(unittest.TestCase):
    def test_native_image_descriptions_use_visual_input_without_acl_or_ground_truth(self):
        with patch("native_indexer_experiment.request") as network:
            result = configuration(include_png_probe=True)
            network.assert_not_called()
        skill = result["skillset"]["skills"][0]
        self.assertEqual(skill["@odata.type"], "#Microsoft.Skills.Custom.ChatCompletionSkill")
        self.assertEqual(skill["uri"], MODEL_URI)
        self.assertEqual(skill["context"], IMAGE_CONTEXT)
        inputs = {item["name"]: item["source"] for item in skill["inputs"]}
        self.assertEqual(inputs["image"], IMAGE_CONTEXT + "/data")
        self.assertEqual(inputs["imageDetail"], "='high'")
        for name in ("imageDetail", "systemMessage", "userMessage"):
            self.assertTrue(inputs[name].startswith("='") and inputs[name].endswith("'"))
            self.assertNotIn("'", inputs[name][2:-1])
        self.assertNotIn("apiKey", skill)
        serialized = json.dumps(skill)
        for forbidden in ("metadata_user_ids", "metadata_group_ids", "expected.json"):
            self.assertNotIn(forbidden, serialized)
        for case in CASES:
            self.assertNotIn(case["marker"], serialized)
        self.assertNotIn("knowledgeStore", result["skillset"])

    def test_each_image_record_projects_real_acl_source_and_site_not_manifest_rules(self):
        result = configuration()
        fields = {field["name"]: field for field in result["index"]["fields"]}
        self.assertTrue(fields["parentId"]["retrievable"])
        self.assertTrue(fields["parentId"]["filterable"])
        self.assertEqual(result["index"]["permissionFilterOption"], "enabled")
        for name in ("userIds", "groupIds"):
            self.assertEqual(fields[name]["permissionFilter"], name)
            self.assertFalse(fields[name]["retrievable"])
            self.assertTrue(fields[name]["filterable"])
        self.assertTrue(fields["SharePointSiteUrl"]["sharepointSiteUrl"])
        self.assertFalse(fields["SharePointSiteUrl"]["retrievable"])
        self.assertFalse(fields["metadata_spo_item_path"]["retrievable"])
        projections = result["skillset"]["indexProjections"]
        self.assertEqual(projections["parameters"]["projectionMode"], "skipIndexingParentDocuments")
        selector = projections["selectors"][0]
        mappings = {mapping["name"]: mapping["source"] for mapping in selector["mappings"]}
        self.assertEqual(selector["sourceContext"], IMAGE_CONTEXT)
        self.assertEqual(selector["targetIndexName"], INDEX)
        for target, source in (
            ("userIds", "metadata_user_ids"), ("groupIds", "metadata_group_ids"),
            ("metadata_spo_item_path", "metadata_spo_item_path"),
            ("SharePointSiteUrl", "metadata_spo_site_url"),
        ):
            self.assertEqual(mappings[target], "/document/" + source)
        self.assertNotIn("id", mappings)
        self.assertNotIn("parentId", mappings)
        self.assertEqual(mappings["content"], IMAGE_CONTEXT + "/description")

    def test_png_is_opt_in_and_each_inert_indexer_only_reads_one_format_in_test_folder(self):
        self.assertEqual(len(configuration()["indexers"]), 2)
        result = configuration(include_png_probe=True)
        self.assertEqual(len(result["indexers"]), 4)
        for source, library in zip(result["datasources"], LIBRARIES.values()):
            self.assertEqual(source["type"], "sharepoint")
            self.assertEqual(source["indexerPermissionOptions"], ["userIds", "groupIds"])
            self.assertTrue(source["container"]["query"].endswith("/" + library + "/" + FOLDER))
            self.assertTrue(source["container"]["query"].startswith("includeFolder=https://"))
            self.assertNotIn("includeLibrary=", source["container"]["query"])
        for indexer in result["indexers"]:
            self.assertTrue(indexer["disabled"])
            self.assertIsNone(indexer["schedule"])
            self.assertEqual(indexer["targetIndexName"], INDEX)
            parameters = indexer["parameters"]
            self.assertEqual(parameters["maxFailedItems"], 0)
            self.assertEqual(parameters["maxFailedItemsPerBatch"], 0)
            self.assertEqual(parameters["configuration"]["imageAction"], "generateNormalizedImages")
            self.assertIn(parameters["configuration"]["indexedFileNameExtensions"], (".pdf", ".png"))
        serialized = json.dumps(result)
        for old in ("teams-acl-demo-sp-native-index\"", "teams-acl-demo-sp-indexed-index",
                    "teams-acl-demo-sp-remote"):
            self.assertNotIn(old, serialized)

    def test_kb_extension_preserves_existing_source_options_and_requires_fresh_expected_state(self):
        baseline = {
            "name": NATIVE_KB, "@odata.etag": '"revision-1"', "@odata.context": "context",
            "models": [], "retrievalReasoningEffort": {"kind": "minimal"},
            "outputMode": "extractiveData", "description": "keep",
            "knowledgeSources": [{"name": NATIVE_KS, "includeReferences": True}],
        }
        result = knowledge_base_update(baseline)
        self.assertEqual(result["ifMatch"], '"revision-1"')
        self.assertEqual(result["body"]["description"], "keep")
        self.assertEqual(result["body"]["knowledgeSources"], [
            {"name": NATIVE_KS, "includeReferences": True}, {"name": KS}])
        self.assertEqual(len(baseline["knowledgeSources"]), 1)
        self.assertNotIn("@odata.etag", result["body"])
        for override in (
            {"name": "remote-kb"}, {"models": [{"model": "other"}]},
            {"knowledgeSources": [{"name": NATIVE_KS}, {"name": KS}]},
            {"@odata.etag": None}, {"outputMode": "answerSynthesis"},
        ):
            with self.subTest(override=override), self.assertRaises(ValueError):
                knowledge_base_update({**baseline, **override})


class NativeChartFixtureTests(unittest.TestCase):
    def assert_chart_values(self, image, expected):
        self.assertEqual(image.size, (1600, 1000))
        for x, label in zip((380, 810, 1240), ("North", "Central", "South")):
            top = next(y for y in range(260, 850) if image.getpixel((x, y)) == (15, 118, 110))
            self.assertAlmostEqual((850 - top) / 590 * 100, expected[label])

    def test_both_formats_for_each_acl_group_and_pdf_evidence_is_only_in_image(self):
        from PIL import Image
        from pypdf import PdfReader

        font = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / "arial.ttf"
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            with patch("native_indexer_experiment.request") as network:
                manifest = prepare_fixtures(output, font)
                network.assert_not_called()
            self.assertEqual(len(manifest["files"]), 4)
            self.assertEqual(
                {(entry["department"], entry["format"]) for entry in manifest["files"]},
                {(department, extension) for department in LIBRARIES for extension in ("pdf", "png")})
            for entry in manifest["files"]:
                path = output / entry["name"]
                self.assertEqual(sha256(path.read_bytes()).hexdigest(), entry["sha256"])
                self.assertEqual(path.stat().st_size, entry["bytes"])
                self.assertNotIn(entry["marker"], path.name)
                if entry["format"] == "pdf":
                    pdf = PdfReader(path)
                    self.assertEqual(len(pdf.pages), 1)
                    self.assertEqual(len(pdf.pages[0].images), 1)
                    text = pdf.pages[0].extract_text()
                    self.assertNotIn(entry["marker"], text)
                    self.assertNotIn("North", text)
                    self.assertNotIn("Central", text)
                    self.assert_chart_values(pdf.pages[0].images[0].image, entry["values"])
                else:
                    with Image.open(path) as image:
                        self.assert_chart_values(image, entry["values"])
                        self.assertEqual(image.info, {})
                        self.assertFalse(image.getexif())
            expected_before = (output / "expected.json").read_bytes()
            with self.assertRaisesRegex(ValueError, "overwrite"):
                prepare_fixtures(output, font)
            self.assertEqual((output / "expected.json").read_bytes(), expected_before)

    def test_missing_font_stops_before_creating_output(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "not-created"
            with self.assertRaisesRegex(ValueError, "font"):
                prepare_fixtures(output, Path(directory) / "missing-font.ttf")
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
