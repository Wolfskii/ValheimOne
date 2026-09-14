#!/usr/bin/env python3
"""Release-safety regressions; no network or publishing calls."""

import copy
import hashlib
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location("nexus_release", Path(__file__).with_name("nexus-release.py"))
sync = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sync)


class ReleaseSafety(unittest.TestCase):
    def release(self):
        return {"tag_name": "v0.13.3", "draft": False, "prerelease": False, "published_at": "2026-09-09T16:16:31Z"}

    def test_only_published_latest_stable_release_is_accepted(self):
        self.assertEqual(sync.release_version(self.release()), "0.13.3")
        for key, value in [("draft", True), ("prerelease", True), ("published_at", None),
                           ("tag_name", "v0.13.3;echo unsafe"), ("tag_name", "v0.13.4-rc1")]:
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                sync.release_version({**self.release(), key: value})
        with self.assertRaisesRegex(ValueError, "downgrade"):
            sync.release_version(self.release(), "v0.13.2")

    def test_existing_file_groups_must_be_unambiguous(self):
        files = [{"id": "a", "name": "ValheimOne 0.13.2"}, {"id": "b", "name": "ValheimOne Full 0.13.2"}]
        self.assertEqual(sync.select_files(files), {"plugin": "a", "full": "b"})
        for invalid in [files[:1], files + [files[0]],
                        [{"id": "../another-mod", "name": "ValheimOne"}, files[1]],
                        [{"id": "a", "name": "Another mod"}, files[1]]]:
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                sync.select_files(invalid)

    def test_retry_does_not_duplicate_existing_or_blocked_versions(self):
        current = {"version": "0.13.3", "category": "main"}
        self.assertEqual(sync.existing_version([current], "0.13.3"), current)
        self.assertIsNone(sync.existing_version([current], "0.13.4"))
        for versions in [[current, current], [{**current, "category": "archived"}],
                         [{**current, "category": "removed"}], [{**current, "category": "unknown"}],
                         [{**current, "version": "0.14.0"}]]:
            with self.subTest(versions=versions), self.assertRaises(ValueError):
                sync.existing_version(versions, "0.13.3")

    def test_altered_artifacts_are_rejected_even_with_same_filenames(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            names = ["ValheimOne-0.13.3.zip", "ValheimOne-full-0.13.3.zip"]
            manifest = []
            for i, name in enumerate(names):
                payload = f"fixture archive {i}".encode()
                (folder / name).write_bytes(payload)
                manifest.append(f"{hashlib.sha256(payload).hexdigest()}  {name}")
            sums = folder / "SHA256SUMS-0.13.3.txt"
            sums.write_text("\n".join(manifest) + "\n")
            release = self.release()
            release["assets"] = [
                {"name": p.name, "size": len(p.read_bytes()), "digest": "sha256:" + hashlib.sha256(p.read_bytes()).hexdigest()}
                for p in folder.iterdir()
            ]
            self.assertEqual(sync.verify_assets(folder, release, "0.13.3"), names)
            (folder / names[0]).write_bytes(b"tampered")
            with self.assertRaisesRegex(ValueError, "digest/size"):
                sync.verify_assets(folder, release, "0.13.3")
            sums.write_text(manifest[0] + "\n" + manifest[0] + "\n")
            with self.assertRaisesRegex(ValueError, "duplicate"):
                sync.verify_assets(folder, release, "0.13.3")

    def test_hash_match_must_belong_to_exact_mod_and_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / "fixture.zip").write_bytes(b"fixture")
            current = {"game_scoped_id": "123"}
            good = {"mod": {"mod_id": 3571}, "file_details": {"file_id": 123}}
            with patch.object(sync, "nexus", return_value=[good]):
                sync.verify_indexed_hash(folder, "fixture.zip", current)
            for records in [[], [{**good, "mod": {"mod_id": 1}}],
                            [{**good, "file_details": {"file_id": 124}}]]:
                with patch.object(sync, "nexus", return_value=records), self.assertRaises(ValueError):
                    sync.verify_indexed_hash(folder, "fixture.zip", current)

    def test_api_redirects_never_forward_credentials(self):
        with self.assertRaisesRegex(RuntimeError, "not forwarded"):
            sync.NoRedirect().redirect_request(None, None, 302, "", {}, "https://unrelated.example")

    def download_status(self):
        return {"data": {
            "mod": {"name": "ValheimOne", "version": "0.13.10", "status": "published"},
            "modFiles": [
                {"fileId": 22082, "version": "0.13.10", "category": "MAIN", "primary": 1,
                 "manager": 0, "requirementsAlert": 1, "scannedV2": "VERIFIED"},
                {"fileId": 22083, "version": "0.13.10", "category": "MAIN", "primary": 0,
                 "manager": 1, "requirementsAlert": 0, "scannedV2": "VERIFIED"},
            ],
        }}

    def verify_downloads(self, response):
        sync.verify_public_downloads(response, "0.13.10", {
            "plugin": {"game_scoped_id": "22082"}, "full": {"game_scoped_id": "22083"},
        })

    def test_matching_archives_do_not_override_quarantine_or_pending_scan(self):
        for status in ["QUARANTINED", "QUEUED", "WAITING_REPORT", "NOT_SCANNED",
                       "REPORT_ERROR", "PARTIAL", "TOO_LARGE", "unknown", None]:
            response = self.download_status()
            response["data"]["modFiles"][1]["scannedV2"] = status
            with self.subTest(status=status), self.assertRaises(ValueError):
                self.verify_downloads(response)

    def test_approved_scan_states_and_historical_quarantine_are_distinct(self):
        for status in ["VERIFIED", "INTERNALLY_VERIFIED", "MANUALLY_VERIFIED"]:
            response = self.download_status()
            response["data"]["modFiles"][1]["scannedV2"] = status
            historical = {**response["data"]["modFiles"][1], "fileId": 21190,
                          "version": "0.13.2", "category": "OLD_VERSION", "scannedV2": "QUARANTINED"}
            response["data"]["modFiles"].append(historical)
            self.verify_downloads(response)

    def test_page_version_and_each_download_control_must_agree(self):
        for field, value in [("version", "0.13.2"), ("status", "hidden"), ("name", "Another mod")]:
            response = self.download_status()
            response["data"]["mod"][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.verify_downloads(response)
        for field, value in [("version", "0.13.2"), ("category", "OLD_VERSION"),
                             ("primary", 0), ("manager", 1), ("requirementsAlert", 0)]:
            response = self.download_status()
            response["data"]["modFiles"][0][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.verify_downloads(response)

    def test_graphql_errors_missing_and_duplicate_file_records_fail_closed(self):
        response = self.download_status()
        missing = copy.deepcopy(response)
        missing["data"]["modFiles"].pop()
        duplicate = copy.deepcopy(response)
        duplicate["data"]["modFiles"].append(duplicate["data"]["modFiles"][0])
        for invalid in [{}, {"data": None}, {"errors": [{"message": "unavailable"}], **response},
                        missing, duplicate]:
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.verify_downloads(invalid)


if __name__ == "__main__":
    unittest.main()
