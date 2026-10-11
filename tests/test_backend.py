"""Backend tests with isolated temporary databases."""

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from backend import MacroService


class BackendTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.database = Path(temp.name) / "data" / "macro.db"
        self.service = MacroService(self.database)
        self.service.initialize()

    def test_catalog_and_empty_database(self) -> None:
        self.assertEqual(self.service.observation_count(), 0)
        self.assertEqual(self.service.find_indicator("GDPC1").name, "Real GDP")
        self.assertTrue(any(
            item.series_id == "GDPC1"
            for item in self.service.indicators("system", "macro")
        ))

    def test_project_persistence_and_chart_settings(self) -> None:
        project_id = self.service.create_project("My Macro Research")["id"]
        self.service.add_indicator(project_id, "GDPC1")
        self.service.add_indicator(project_id, "GDPC1")

        settings = {"period": "10Y", "chart_type": "Area", "grid": False}
        self.service.save_chart_settings(project_id, "GDPC1", settings)

        fresh = MacroService(self.database)
        fresh.initialize()
        self.assertEqual(fresh.get_project(project_id)["series_ids"], ["GDPC1"])
        self.assertEqual(fresh.chart_settings(project_id, "GDPC1"), settings)

    def test_rename_remove_and_delete(self) -> None:
        project_id = self.service.create_project("Test")["id"]
        self.service.rename_project(project_id, "Renamed")
        self.assertEqual(self.service.get_project(project_id)["name"], "Renamed")

        self.service.add_indicator(project_id, "DGS10")
        self.service.remove_indicator(project_id, "DGS10")
        self.assertEqual(self.service.get_project(project_id)["series_ids"], [])

        self.service.delete_project(project_id)
        self.assertEqual(self.service.list_projects(), [])

    def test_existing_observations_survive_reinitialization(self) -> None:
        with closing(sqlite3.connect(self.database)) as conn, conn:
            conn.execute(
                "INSERT INTO observations (series_id, date, value) VALUES (?, ?, ?)",
                ("GDPC1", "2025-01-01", 123.5),
            )

        fresh = MacroService(self.database)
        fresh.initialize()
        self.assertEqual(fresh.observation_count("GDPC1"), 1)
        self.assertEqual(fresh.observation_count("CPIAUCSL"), 0)

    def test_duplicate_names_and_unknown_series(self) -> None:
        project_id = self.service.create_project("Project")["id"]

        with self.assertRaises(ValueError):
            self.service.create_project("project")

        with self.assertRaises(KeyError):
            self.service.add_indicator(project_id, "UNKNOWN_SERIES")


if __name__ == "__main__":
    unittest.main()