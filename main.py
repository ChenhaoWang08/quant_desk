"""QuantDesk application entry point."""

import sqlite3
import sys
from pathlib import Path

from PySide6.QtWidgets import QApplication, QMessageBox
from backend import MacroService
from frontend import MainWindow


def main() -> int:
    app = QApplication(sys.argv)

    try:
        backend = MacroService(
            Path(__file__).resolve().parent / "data" / "macro.db"
        )
        backend.initialize()
    except (OSError, ValueError, sqlite3.Error) as exc:
        QMessageBox.critical(None, "QuantDesk startup error", str(exc))
        return 1

    window = MainWindow(backend)
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())