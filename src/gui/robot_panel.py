"""Robot status and control panel."""

from PyQt6.QtWidgets import (
    QGroupBox,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


class RobotPanel(QWidget):
    """Panel for monitoring and controlling connected robots."""

    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)

        # Turtle section
        turtle_group = QGroupBox("Turtle")
        turtle_layout = QVBoxLayout(turtle_group)
        self._turtle_status = QLabel("Status: Disconnected")
        turtle_layout.addWidget(self._turtle_status)
        self._turtle_connect_btn = QPushButton("Connect")
        turtle_layout.addWidget(self._turtle_connect_btn)
        layout.addWidget(turtle_group)

        # Thymio section
        thymio_group = QGroupBox("Thymios")
        thymio_layout = QVBoxLayout(thymio_group)
        self._thymio_status = QLabel("Status: Disconnected")
        thymio_layout.addWidget(self._thymio_status)
        self._thymio_connect_btn = QPushButton("Connect")
        thymio_layout.addWidget(self._thymio_connect_btn)
        layout.addWidget(thymio_group)

        # Tree section
        tree_group = QGroupBox("Tree")
        tree_layout = QVBoxLayout(tree_group)
        self._tree_status = QLabel("Status: Disconnected")
        tree_layout.addWidget(self._tree_status)
        self._tree_connect_btn = QPushButton("Connect")
        tree_layout.addWidget(self._tree_connect_btn)
        layout.addWidget(tree_group)

        # Gateway section
        gateway_group = QGroupBox("ESP-NOW Gateway")
        gateway_layout = QVBoxLayout(gateway_group)
        self._gateway_status = QLabel("Status: Disconnected")
        gateway_layout.addWidget(self._gateway_status)
        self._gateway_connect_btn = QPushButton("Connect Gateway")
        gateway_layout.addWidget(self._gateway_connect_btn)
        layout.addWidget(gateway_group)

        layout.addStretch()
