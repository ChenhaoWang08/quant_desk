"""Optional Qt smoke test; requires PySide6 and pyqtgraph."""

import tempfile
import unittest
from pathlib import Path

from backend import MacroService

try:
    from PySide6.QtWidgets import QApplication
    from frontend import MainWindow
except ImportError:
    HAS_QT = False
else:
    HAS_QT = True


@unittest.skipUnless(HAS_QT, "PySide6/pyqtgraph not installed in this environment")
class UISmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_navigation_and_persisted_chart_settings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = MacroService(Path(tmp) / "data" / "macro.db")
            service.initialize()
            window = MainWindow(service)

            try:
                window.explorer.select_indicator("GDPC1")
                self.assertEqual(window.workspace.title.text(), "Real GDP")
                self.assertEqual(window.workspace.fields["count"].text(), "0")
                self.assertEqual(
                    window.workspace.fields["api"].text(), "Not configured"
                )

                project_id = service.create_project("UI Test")["id"]
                service.add_indicator(project_id, "GDPC1")
                window.sidebar.set_projects(service.list_projects())
                window._switch_section("custom", project_id)
                window.explorer.select_indicator("GDPC1")
                window.workspace.period_buttons["10Y"].click()

                self.assertEqual(
                    service.chart_settings(project_id, "GDPC1")["period"],
                    "10Y",
                )
            finally:
                window.close()


if __name__ == "__main__":
    unittest.main()