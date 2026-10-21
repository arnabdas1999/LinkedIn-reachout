from pathlib import Path
import tempfile
import unittest

from lead_data import clean_csv_file, csv_text, read_clean_csv, repair_contact


class LeadDataTests(unittest.TestCase):
    def test_missing_names_use_full_name_without_dropping_name_tokens(self):
        for full, expected in (("Wing Lau", "Wing"), ("Guru Huchachar", "Guru"),
                               ("Deacon Mike", "Deacon"), ("Kalees (kkaruppa)", "Kalees"),
                               ("Dr. Faisal Ahmad (Ph.d)", "Faisal")):
            with self.subTest(full=full):
                row = repair_contact({"Full Name": full, "First Name": "", "Last Name": ""})
                self.assertEqual(row["First Name"], expected)
                self.assertEqual(row["Full Name"], full)

    def test_existing_names_and_unicode_are_preserved(self):
        row = repair_contact({"Full Name": "Md Ashraful Alam", "First Name": "Ashraful", "Last Name": "Alam"})
        self.assertEqual(row["First Name"], "Ashraful")
        self.assertEqual(repair_contact({"Full Name": "Seán '", "First Name": "Seán"})["First Name"], "Seán")

    def test_missing_last_name_and_title_parser_error(self):
        row = repair_contact({"Full Name": "King Lam", "First Name": "Lam", "Last Name": ""})
        self.assertEqual((row["First Name"], row["Last Name"]), ("King", "Lam"))
        row = repair_contact({"Full Name": "Kalees (kkaruppa)", "First Name": "", "Last Name": "Kalees"})
        self.assertEqual((row["First Name"], row["Last Name"]), ("Kalees", ""))
        for name in ("Belle .", "Aashna .", "Seán '"):
            self.assertEqual(repair_contact({"Full Name": name}).get("Last Name", ""), "")

    def test_country_is_not_used_as_city_and_no_location_is_invented(self):
        row = repair_contact({"City": " United States ", "Country": "United States", "State or Province": ""})
        self.assertEqual(row["City"], "")
        self.assertEqual(row["State or Province"], "")
        self.assertEqual(repair_contact({"City": " New\u00a0 York  ", "State or Province": "New York"})["City"], "New York")

    def test_cleanup_merges_duplicates_keeps_order_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "source.csv"
            headers = ["First Name", "Full Name", "Company", "City", "LinkedIn Profile"]
            original = csv_text(headers, [
                dict(zip(headers, ("", "Wing  Lau", "Workday", "", "https://linkedin.com/in/Wing/?trk=test"))),
                dict(zip(headers, ("Wing", "Wing Lau", "Workday", "San Francisco", "https://www.linkedin.com/in/wing/"))),
                dict(zip(headers, ("Alex", "Alex Example", "Oracle", "Boston", "https://linkedin.com/in/alex/"))),
            ])
            path.write_text(original, encoding="utf-8-sig", newline="")
            report = clean_csv_file(path, root / "backups")
            self.assertEqual(report["duplicates_removed"], 1)
            self.assertEqual(Path(report["backup"]).read_text(encoding="utf-8-sig"), original.replace("\r\n", "\n"))
            _, rows, _ = read_clean_csv(path)
            self.assertEqual([r["First Name"] for r in rows], ["Wing", "Alex"])
            self.assertEqual(rows[0]["City"], "San Francisco")
            content = path.read_bytes()
            again = clean_csv_file(path, root / "backups")
            self.assertEqual(again["changes"], [])
            self.assertEqual(again["duplicates_removed"], 0)
            self.assertEqual(path.read_bytes(), content)
            self.assertEqual(len(list((root / "backups").iterdir())), 1)

    def test_invalid_urls_and_conflicts_are_reported_without_inventing_data(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "source.csv"
            path.write_text("Full Name,Company,LinkedIn Profile\nAlex Example,Oracle,https://linkedin.com/in/alex/\nAlex Example,Other,https://linkedin.com/in/alex/\nMorgan Example,Workday,https://evil.test/in/morgan/\n", encoding="utf-8")
            _, rows, report = read_clean_csv(path)
            self.assertEqual(rows[0]["Company"], "Oracle")
            self.assertEqual(rows[1]["LinkedIn Profile"], "https://evil.test/in/morgan/")
            self.assertEqual(report["invalid_url_rows"], [4])
            self.assertEqual(report["conflicts"][0]["column"], "Company")


if __name__ == "__main__":
    unittest.main()
