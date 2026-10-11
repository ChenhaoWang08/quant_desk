"""QuantDesk desktop theme; static QSS with no external dependencies."""

STYLE = """
QWidget {
    font-family: 'Segoe UI';
    font-size: 13px;
    color: #273345;
    background-color: #ffffff;
}

QFrame#sidebar { background: #f3f5f8; }
QFrame#explorer { background: #fbfcfe; }

QFrame#card {
    background: #ffffff;
    border: 1px solid #e3e8ef;
    border-radius: 10px;
}

QLabel#brand {
    font-size: 21px;
    font-weight: 700;
    color: #1f2937;
}

QLabel#sectionHeading {
    font-size: 11px;
    font-weight: 700;
    color: #64748b;
}

QLabel#workspaceTitle {
    font-size: 25px;
    font-weight: 700;
}

QLabel#emptyTitle {
    font-size: 18px;
    font-weight: 600;
}

QLabel#muted {
    font-size: 12px;
    color: #718096;
}

QLabel { background: transparent; }

QListWidget, QTreeWidget {
    background: transparent;
    border: none;
    outline: none;
}

QListWidget::item, QTreeWidget::item {
    padding: 7px 8px;
    border-radius: 6px;
}

QListWidget::item:selected, QTreeWidget::item:selected {
    color: #16487c;
    background: #dfeaf8;
}

QListWidget::item:hover, QTreeWidget::item:hover {
    background: #eaf0f7;
}

QLineEdit, QComboBox {
    background: #ffffff;
    border: 1px solid #d8dfe9;
    border-radius: 6px;
    padding: 7px;
}

QPushButton {
    background: #ffffff;
    border: 1px solid #d8dfe9;
    border-radius: 6px;
    padding: 7px 10px;
}

QPushButton:hover { background: #eaf0f8; }

QPushButton:disabled {
    color: #a0aab8;
    background: #f5f6f8;
}

QPushButton:checked {
    color: #15518a;
    background: #deebfa;
    border: 1px solid #aac9ed;
}

QPushButton#subtleButton { padding: 5px 7px; }
QSplitter::handle { background: #e3e8ef; }

QStatusBar {
    background: #f6f8fb;
    color: #66788b;
}
"""