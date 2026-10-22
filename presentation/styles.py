from PyQt6.QtWidgets import QApplication

APP_STYLESHEET = """
QWidget {
    background-color: #FFFFFF;
    color: #1E293B;
}

QDialog, QMessageBox {
    background-color: #FFFFFF;
    color: #0F172A;
}

QMessageBox QLabel {
    background-color: transparent;
    color: #0F172A;
    font-size: 13px;
    padding: 4px;
}

QMessageBox QPushButton {
    background-color: #2563EB;
    color: #FFFFFF;
    border-radius: 6px;
    font-weight: 600;
    padding: 6px 18px;
    min-width: 75px;
    font-size: 12px;
    border: none;
}

QMessageBox QPushButton:hover {
    background-color: #1D4ED8;
}
"""

def setup_application_theme(app: QApplication):
    """Cấu hình style Fusion hiện đại và màu nền trắng đồng bộ cho toàn bộ ứng dụng."""
    try:
        app.setStyle("Fusion")
    except Exception:
        pass
    app.setStyleSheet(APP_STYLESHEET)
