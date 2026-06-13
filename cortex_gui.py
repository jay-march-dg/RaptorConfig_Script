from __future__ import annotations

import ast
import csv
import ipaddress
import locale
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QProcess,
    QProcessEnvironment,
    QSettings,
    QSortFilterProxyModel,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import QColor, QBrush, QPalette
from PySide6.QtWidgets import (
    QApplication,
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QCompleter,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTableView,
    QTabWidget,
    QVBoxLayout,
    QWidget,
    QInputDialog,
)

APP_ENCODING = locale.getpreferredencoding(False) or "utf-8"
BASE_DIR = Path(__file__).resolve().parent

DEFAULT_HEADERS = ["device_name", "device_type", "ip_address", "gateway"]

DEFAULT_SCRIPT_NAME = "upload_cortex.py"
DEFAULT_DEVICE_LIST_NAME = "deviceList.csv"

FALLBACK_ADAPTER_NAME = "Ethernet 6"
FALLBACK_DEFAULT_DEVICE_IP = "192.168.7.3"
FALLBACK_PANEL_TYPES = ["14", "28", "30", "26S(3x3)", "10S(5x5)", "26S(1x1)"]

RUN_MODE_SPECS: list[tuple[str, str]] = [
    ("configure", "Configure Device"),
    ("diag", "Diag / Find & Correct IP (--diag)"),
    ("pingall", "Scan Subnets (--pingall)"),
    ("reboot", "Reboot & Verify (--reboot)"),
    ("verifyall", "Verify by Prefix (--verifyall)"),
    ("configall", "Configure by Prefix (--configall)"),
]

SINGLE_DEVICE_MODES = {"configure", "diag", "pingall", "reboot"}
SINGLE_DEVICE_MODES_REQUIRING_TYPE = {"configure", "diag", "pingall", "reboot"}
PREFIX_MODES = {"verifyall", "configall"}


def is_prefix_mode(mode: str) -> bool:
    return mode in PREFIX_MODES


def is_single_device_mode(mode: str) -> bool:
    return mode in SINGLE_DEVICE_MODES


def should_keep_typed_prefix(mode: str, current_text: str, available_names: list[str]) -> bool:
    return is_prefix_mode(mode) and bool(current_text) and current_text not in available_names


def clean_path(text: str) -> Path:
    return Path(text).expanduser()


def script_dir_from_path(path: Path) -> Path:
    return path.parent if path.suffix else path


def is_valid_ipv4(value: str) -> bool:
    try:
        ipaddress.IPv4Address(value.strip())
        return True
    except Exception:
        return False


def derive_gateway(ip_address: str) -> str:
    octets = ip_address.strip().split(".")
    if len(octets) != 4:
        raise ValueError("Invalid IPv4 address")
    octets[3] = "1"
    return ".".join(octets)


def join_command(parts: list[str]) -> str:
    return " ".join(f'"{part}"' if " " in part else part for part in parts)


def canonicalize_headers(headers: list[str], required_headers: list[str] | None = None) -> list[str]:
    """Return headers in a stable order with required columns first."""
    required = list(required_headers or DEFAULT_HEADERS)
    cleaned = [item.strip() for item in headers if item and item.strip()]

    ordered: list[str] = []
    seen: set[str] = set()

    for name in required:
        if name in cleaned and name not in seen:
            ordered.append(name)
            seen.add(name)

    for name in cleaned:
        if name not in seen:
            ordered.append(name)
            seen.add(name)

    return ordered


@dataclass
class CortexScriptMetadata:
    adapter_name: str = FALLBACK_ADAPTER_NAME
    default_device_ip: str = FALLBACK_DEFAULT_DEVICE_IP
    valid_panel_types: list[str] = field(default_factory=lambda: FALLBACK_PANEL_TYPES.copy())


def read_cortex_script_metadata(script_path: Path) -> CortexScriptMetadata:
    metadata = CortexScriptMetadata()

    if not script_path.exists():
        return metadata

    try:
        text = script_path.read_text(encoding="utf-8")
        tree = ast.parse(text, filename=str(script_path))
    except Exception:
        return metadata

    values: dict[str, Any] = {}

    def maybe_store(name: str, value_node: ast.AST) -> None:
        try:
            values[name] = ast.literal_eval(value_node)
        except Exception:
            return

    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in {
                    "ADAPTER_NAME",
                    "DEFAULT_DEVICE_IP",
                    "VALID_PANEL_TYPES",
                }:
                    maybe_store(target.id, node.value)
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and node.target.id in {
                "ADAPTER_NAME",
                "DEFAULT_DEVICE_IP",
                "VALID_PANEL_TYPES",
            } and node.value is not None:
                maybe_store(node.target.id, node.value)

    adapter_name = values.get("ADAPTER_NAME")
    default_device_ip = values.get("DEFAULT_DEVICE_IP")
    valid_panel_types = values.get("VALID_PANEL_TYPES")

    if isinstance(adapter_name, str) and adapter_name.strip():
        metadata.adapter_name = adapter_name.strip()

    if isinstance(default_device_ip, str) and default_device_ip.strip():
        metadata.default_device_ip = default_device_ip.strip()

    if isinstance(valid_panel_types, list) and all(isinstance(v, str) for v in valid_panel_types):
        metadata.valid_panel_types = [v.strip() for v in valid_panel_types if v.strip()] or FALLBACK_PANEL_TYPES.copy()

    return metadata


class DeviceCsvModel(QAbstractTableModel):
    dirtyChanged = Signal(bool)

    def __init__(
        self,
        parent: QWidget | None = None,
        required_headers: list[str] | None = None,
        valid_device_types: list[str] | None = None,
    ) -> None:
        super().__init__(parent)
        self.required_headers: list[str] = (required_headers or DEFAULT_HEADERS).copy()
        self.valid_device_types: list[str] = (valid_device_types or FALLBACK_PANEL_TYPES).copy()

        self.headers: list[str] = self.required_headers.copy()
        self.rows: list[dict[str, str]] = []
        self.csv_path: Path | None = None
        self._dirty = False

    @property
    def dirty(self) -> bool:
        return self._dirty

    def _set_dirty(self, value: bool) -> None:
        if self._dirty != value:
            self._dirty = value
            self.dirtyChanged.emit(value)

    def set_valid_device_types(self, device_types: list[str]) -> None:
        self.valid_device_types = device_types.copy()
        if self.rowCount() and self.columnCount():
            self.dataChanged.emit(
                self.index(0, 0),
                self.index(self.rowCount() - 1, self.columnCount() - 1),
                [Qt.ItemDataRole.BackgroundRole, Qt.ItemDataRole.ForegroundRole],
            )

    def _device_type_valid(self, value: str) -> bool:
        value = value.strip()
        if not value:
            return True
        if not self.valid_device_types:
            return True
        return value in self.valid_device_types

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        if parent.isValid():
            return 0
        return len(self.rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        if parent.isValid():
            return 0
        return len(self.headers)

    def headerData(
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> Any:
        if role != Qt.ItemDataRole.DisplayRole:
            return None

        if orientation == Qt.Orientation.Horizontal:
            try:
                return self.headers[section]
            except IndexError:
                return None

        return str(section + 1)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid():
            return None

        row = self.rows[index.row()]
        column_name = self.headers[index.column()]
        value = row.get(column_name, "")

        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.EditRole):
            return value

        if role == Qt.ItemDataRole.BackgroundRole and self._cell_invalid(index.row(), column_name):
            return QBrush(QColor("#4b1f27"))

        if role == Qt.ItemDataRole.ForegroundRole and self._cell_invalid(index.row(), column_name):
            return QBrush(QColor("#ffb3bd"))

        return None

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        return (
            Qt.ItemFlag.ItemIsEnabled
            | Qt.ItemFlag.ItemIsSelectable
            | Qt.ItemFlag.ItemIsEditable
        )

    def setData(
        self,
        index: QModelIndex,
        value: Any,
        role: int = Qt.ItemDataRole.EditRole,
    ) -> bool:
        if not index.isValid() or role != Qt.ItemDataRole.EditRole:
            return False

        column_name = self.headers[index.column()]
        self.rows[index.row()][column_name] = str(value).strip()
        self._set_dirty(True)
        self.dataChanged.emit(
            index,
            index,
            [
                Qt.ItemDataRole.DisplayRole,
                Qt.ItemDataRole.EditRole,
                Qt.ItemDataRole.BackgroundRole,
                Qt.ItemDataRole.ForegroundRole,
            ],
        )
        return True

    def insertRows(
        self,
        row: int,
        count: int,
        parent: QModelIndex = QModelIndex(),
    ) -> bool:
        if parent.isValid():
            return False

        row = max(0, min(row, len(self.rows)))
        self.beginInsertRows(QModelIndex(), row, row + count - 1)
        blank = {header: "" for header in self.headers}
        for _ in range(count):
            self.rows.insert(row, blank.copy())
        self.endInsertRows()
        self._set_dirty(True)
        return True

    def removeRows(
        self,
        row: int,
        count: int,
        parent: QModelIndex = QModelIndex(),
    ) -> bool:
        if parent.isValid() or row < 0 or row + count > len(self.rows):
            return False

        self.beginRemoveRows(QModelIndex(), row, row + count - 1)
        del self.rows[row: row + count]
        self.endRemoveRows()
        self._set_dirty(True)
        return True

    def add_column(self, name: str) -> bool:
        name = name.strip()
        if not name or name in self.headers:
            return False

        insert_at = len(self.headers)
        self.beginInsertColumns(QModelIndex(), insert_at, insert_at)
        self.headers.append(name)
        for row in self.rows:
            row[name] = ""
        self.endInsertColumns()
        self._set_dirty(True)
        return True

    def load_csv(self, path: Path) -> None:
        headers = self.required_headers.copy()
        rows: list[dict[str, str]] = []

        if path.exists():
            with path.open("r", newline="", encoding="utf-8-sig") as handle:
                reader = csv.DictReader(handle)
                existing_headers = [h.strip() for h in (reader.fieldnames or []) if h and h.strip()]
                if existing_headers:
                    headers = canonicalize_headers(existing_headers, self.required_headers)

                for required in self.required_headers:
                    if required not in headers:
                        headers.append(required)

                for source_row in reader:
                    row = {header: (source_row.get(header, "") or "").strip() for header in headers}
                    rows.append(row)

        self.beginResetModel()
        self.headers = headers
        self.rows = rows
        self.csv_path = path
        self.endResetModel()
        self._set_dirty(False)

    def save_csv(self, path: Path | None = None) -> None:
        if path is None:
            if self.csv_path is None:
                raise ValueError("No CSV path provided.")
            path = self.csv_path

        path.parent.mkdir(parents=True, exist_ok=True)

        with path.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=self.headers)
            writer.writeheader()
            for row in self.rows:
                if not any((row.get(h, "") or "").strip() for h in self.headers):
                    continue
                writer.writerow({header: row.get(header, "") for header in self.headers})

        self.csv_path = path
        self._set_dirty(False)

    def device_names(self) -> list[str]:
        names: list[str] = []
        for row in self.rows:
            value = row.get("device_name", "").strip()
            if value:
                names.append(value)
        return names

    def find_device(self, device_name: str) -> dict[str, str] | None:
        target = device_name.strip()
        for row in self.rows:
            if row.get("device_name", "").strip() == target:
                return row.copy()
        return None

    def find_device_row_index(self, device_name: str) -> int | None:
        target = device_name.strip()
        for idx, row in enumerate(self.rows):
            if row.get("device_name", "").strip() == target:
                return idx
        return None

    def validate_rows(self) -> list[str]:
        errors: list[str] = []
        seen_names: set[str] = set()

        for idx, row in enumerate(self.rows, start=1):
            has_any = any((row.get(h, "") or "").strip() for h in self.headers)
            if not has_any:
                continue

            name = row.get("device_name", "").strip()
            device_type = row.get("device_type", "").strip()
            ip_address = row.get("ip_address", "").strip()

            if not name:
                errors.append(f"Row {idx}: device_name is required.")
            elif name in seen_names:
                errors.append(f"Row {idx}: duplicate device_name '{name}'.")
            else:
                seen_names.add(name)

            if not ip_address:
                errors.append(f"Row {idx}: ip_address is required.")
            elif not is_valid_ipv4(ip_address):
                errors.append(f"Row {idx}: invalid ip_address '{ip_address}'.")

            if device_type and not self._device_type_valid(device_type):
                errors.append(
                    f"Row {idx}: invalid device_type '{device_type}'. "
                    f"Valid types: {', '.join(self.valid_device_types)}"
                )

        return errors

    def _cell_invalid(self, row_index: int, column_name: str) -> bool:
        row = self.rows[row_index]
        has_any = any((row.get(h, "") or "").strip() for h in self.headers)
        value = row.get(column_name, "").strip()

        if not has_any:
            return False

        if column_name in {"device_name", "ip_address"} and not value:
            return True

        if column_name == "ip_address" and value and not is_valid_ipv4(value):
            return True

        if column_name == "device_type" and value and not self._device_type_valid(value):
            return True

        return False


class ContainsFilterProxyModel(QSortFilterProxyModel):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._needle = ""

    def setFilterText(self, text: str) -> None:
        self._needle = (text or "").lower().strip()
        self.invalidateFilter()

    def filterAcceptsRow(self, source_row: int, source_parent: QModelIndex) -> bool:
        if not self._needle:
            return True

        model = self.sourceModel()
        if model is None:
            return True

        for column in range(model.columnCount()):
            index = model.index(source_row, column, source_parent)
            value = str(model.data(index, Qt.ItemDataRole.DisplayRole) or "")
            if self._needle in value.lower():
                return True

        return False


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.settings = QSettings("LocalAutomation", "CortexConfiguratorGUI")

        self.metadata = CortexScriptMetadata()
        self.model = DeviceCsvModel(
            self,
            required_headers=DEFAULT_HEADERS,
            valid_device_types=self.metadata.valid_panel_types,
        )

        self.selector_proxy = ContainsFilterProxyModel(self)
        self.selector_proxy.setSourceModel(self.model)

        self.manager_proxy = ContainsFilterProxyModel(self)
        self.manager_proxy.setSourceModel(self.model)

        self.process = QProcess(self)
        self.process.setProcessChannelMode(QProcess.ProcessChannelMode.SeparateChannels)

        self._syncing_device_selection = False

        self._build_ui()
        self._connect_signals()
        self._load_settings()
        self._update_window_title()
        self.refresh_device_combo()
        self.update_device_details()
        self._update_mode_ui()
        self._set_run_state(False)

    def _build_ui(self) -> None:
        self.setWindowTitle("Cortex Configurator")
        self.resize(1280, 860)
        self.setMinimumSize(1100, 760)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_config_tab(), "Configurator")
        self.tabs.addTab(self._build_settings_tab(), "Settings")
        self.tabs.addTab(self._build_device_manager_tab(), "Device List Manager")
        self.setCentralWidget(self.tabs)

        self.statusBar().showMessage("Ready")

    def _build_config_tab(self) -> QWidget:
        tab = QWidget()
        root = QVBoxLayout(tab)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(14)

        hero = QFrame()
        hero.setObjectName("heroCard")
        hero_layout = QVBoxLayout(hero)
        hero_layout.setContentsMargins(20, 18, 20, 18)

        title = QLabel("Cortex Device Configurator")
        title.setObjectName("titleLabel")
        subtitle = QLabel(
            "Developed by Jay March"
        )
        subtitle.setObjectName("subtitleLabel")
        subtitle.setWordWrap(True)

        hero_layout.addWidget(title)
        hero_layout.addWidget(subtitle)
        root.addWidget(hero)

        content = QHBoxLayout()
        content.setSpacing(14)

        selector_group = QGroupBox("Device Selection")
        selector_layout = QVBoxLayout(selector_group)

        selector_top = QHBoxLayout()
        self.selector_search_edit = QLineEdit()
        self.selector_search_edit.setPlaceholderText("Search devices...")
        self.selector_count_label = QLabel("0 visible / 0 total")
        reload_csv_btn = QPushButton("Reload CSV")

        selector_top.addWidget(QLabel("Search"))
        selector_top.addWidget(self.selector_search_edit, 1)
        selector_top.addWidget(self.selector_count_label)
        selector_top.addWidget(reload_csv_btn)

        self.selector_table = QTableView()
        self.selector_table.setModel(self.selector_proxy)
        self.selector_table.setAlternatingRowColors(True)
        self.selector_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.selector_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.selector_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.selector_table.setSortingEnabled(True)
        self.selector_table.verticalHeader().setVisible(False)
        self.selector_table.horizontalHeader().setStretchLastSection(True)
        self.selector_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)

        selector_layout.addLayout(selector_top)
        selector_layout.addWidget(self.selector_table, 1)

        right_col = QVBoxLayout()

        self.device_combo = QComboBox()
        self.device_combo.setEditable(True)
        self.device_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.device_combo.lineEdit().setPlaceholderText("Select an exact device_name or type a prefix")
        self.device_completer = QCompleter(self.device_combo.model(), self)
        self.device_completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.device_completer.setFilterMode(Qt.MatchFlag.MatchContains)
        self.device_combo.setCompleter(self.device_completer)

        self.mode_combo = QComboBox()
        for key, label in RUN_MODE_SPECS:
            self.mode_combo.addItem(label, key)

        self.device_type_combo = QComboBox()
        self.device_type_combo.setEditable(True)
        self.device_type_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.device_type_combo.lineEdit().setPlaceholderText("Optional or required depending on mode")

        self.device_count_label = QLabel("0 devices loaded")
        self.type_value = QLabel("-")
        self.target_ip_value = QLabel("-")
        self.gateway_value = QLabel("-")
        self.template_value = QLabel("-")
        self.template_value.setWordWrap(True)
        self.default_ip_value = QLabel("-")

        device_group = QGroupBox("Selected Target")
        device_layout = QFormLayout(device_group)
        device_layout.addRow("Device / Prefix", self.device_combo)
        device_layout.addRow("Run Mode", self.mode_combo)
        device_layout.addRow("Type Override / Filter", self.device_type_combo)
        device_layout.addRow("Devices Loaded", self.device_count_label)
        device_layout.addRow("Resolved Type", self.type_value)
        device_layout.addRow("Target IP", self.target_ip_value)
        device_layout.addRow("Gateway (derived)", self.gateway_value)
        device_layout.addRow("Template Preview", self.template_value)
        device_layout.addRow("Default IP", self.default_ip_value)

        self.run_button = QPushButton("Run Configuration")
        self.run_button.setObjectName("primaryButton")
        self.open_button = QPushButton("Open Web UI")
        self.stop_button = QPushButton("Stop")
        self.stop_button.setObjectName("dangerButton")

        action_row = QHBoxLayout()
        action_row.addWidget(self.run_button)
        action_row.addWidget(self.open_button)
        action_row.addWidget(self.stop_button)

        self.mode_hint_label = QLabel("")
        self.mode_hint_label.setWordWrap(True)
        self.mode_hint_label.setObjectName("subtitleLabel")

        right_col.addWidget(device_group)
        right_col.addLayout(action_row)
        right_col.addWidget(self.mode_hint_label)
        right_col.addStretch()

        content.addWidget(selector_group, 3)
        right_panel = QWidget()
        right_panel.setLayout(right_col)
        content.addWidget(right_panel, 2)

        root.addLayout(content, 1)

        self.use_device_ip_check = QCheckBox("Use device subnet first (--a2)")
        self.rdp_check = QCheckBox("RDP mode (--rdp)")

        flags_group = QGroupBox("Command Flags")
        flags_layout = QHBoxLayout(flags_group)
        flags_layout.addWidget(self.use_device_ip_check)
        flags_layout.addWidget(self.rdp_check)
        flags_layout.addStretch()

        root.addWidget(flags_group)

        self.log_output = QPlainTextEdit()
        self.log_output.setReadOnly(True)
        self.log_output.setMaximumBlockCount(5000)

        log_group = QGroupBox("Execution Log")
        log_layout = QVBoxLayout(log_group)
        log_layout.addWidget(self.log_output)
        root.addWidget(log_group, 1)

        reload_csv_btn.clicked.connect(self.reload_csv)
        return tab

    def _build_settings_tab(self) -> QWidget:
        tab = QWidget()
        root = QVBoxLayout(tab)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(14)

        intro = QLabel(
            "upload_cortex.py reads deviceList.csv and Cortexsettings templates from its own folder. "
            "Because of that, this GUI derives the Device List and Template Dir directly from the selected script."
        )
        intro.setWordWrap(True)
        intro.setObjectName("subtitleLabel")
        root.addWidget(intro)

        self.script_edit = QLineEdit()
        self.script_edit.setPlaceholderText(str(BASE_DIR / DEFAULT_SCRIPT_NAME))
        browse_script_btn = QPushButton("Browse...")

        self.device_list_edit = QLineEdit()
        self.device_list_edit.setReadOnly(True)

        self.template_dir_edit = QLineEdit()
        self.template_dir_edit.setReadOnly(True)

        paths_group = QGroupBox("Project Paths")
        paths_layout = QGridLayout(paths_group)

        paths_layout.addWidget(QLabel("Script"), 0, 0)
        paths_layout.addWidget(self.script_edit, 0, 1)
        paths_layout.addWidget(browse_script_btn, 0, 2)

        paths_layout.addWidget(QLabel("Device List"), 1, 0)
        paths_layout.addWidget(self.device_list_edit, 1, 1)
        reload_csv_btn = QPushButton("Reload CSV")
        paths_layout.addWidget(reload_csv_btn, 1, 2)

        paths_layout.addWidget(QLabel("Template Dir"), 2, 0)
        paths_layout.addWidget(self.template_dir_edit, 2, 1, 1, 2)

        root.addWidget(paths_group)

        self.adapter_edit = QLineEdit()
        self.adapter_edit.setReadOnly(True)

        self.default_ip_edit = QLineEdit()
        self.default_ip_edit.setReadOnly(True)

        self.valid_types_edit = QLineEdit()
        self.valid_types_edit.setReadOnly(True)

        metadata_group = QGroupBox("Script Metadata")
        metadata_layout = QGridLayout(metadata_group)
        metadata_layout.addWidget(QLabel("Adapter Name"), 0, 0)
        metadata_layout.addWidget(self.adapter_edit, 0, 1)
        metadata_layout.addWidget(QLabel("Default Device IP"), 1, 0)
        metadata_layout.addWidget(self.default_ip_edit, 1, 1)
        metadata_layout.addWidget(QLabel("Valid Panel Types"), 2, 0)
        metadata_layout.addWidget(self.valid_types_edit, 2, 1)
        root.addWidget(metadata_group)

        note = QLabel(
            "Notes:\n"
            "• Adapter name is currently hardcoded in upload_cortex.py.\n"
            "• The Open Web UI button runs: upload_cortex.py <device_name> --open\n"
            "• --configall is built with --a2 automatically by the GUI."
        )
        note.setWordWrap(True)
        note.setObjectName("subtitleLabel")
        root.addWidget(note)

        button_row = QHBoxLayout()
        button_row.addStretch()

        use_defaults_btn = QPushButton("Use GUI Folder Defaults")
        save_settings_btn = QPushButton("Save Settings")

        button_row.addWidget(use_defaults_btn)
        button_row.addWidget(save_settings_btn)

        root.addLayout(button_row)
        root.addStretch()

        browse_script_btn.clicked.connect(self.browse_script)
        reload_csv_btn.clicked.connect(self.reload_csv)
        use_defaults_btn.clicked.connect(self.use_gui_folder_defaults)
        save_settings_btn.clicked.connect(self.save_settings_now)

        return tab

    def _build_device_manager_tab(self) -> QWidget:
        tab = QWidget()
        root = QVBoxLayout(tab)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(12)

        toolbar_row = QHBoxLayout()
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Search all columns...")

        add_row_btn = QPushButton("Add Row")
        delete_row_btn = QPushButton("Delete Selected")
        add_column_btn = QPushButton("Add Column")
        save_csv_btn = QPushButton("Save CSV")
        reload_csv_btn = QPushButton("Reload CSV")
        self.manager_count_label = QLabel("0 rows")

        toolbar_row.addWidget(QLabel("Search"))
        toolbar_row.addWidget(self.search_edit, 1)
        toolbar_row.addWidget(add_row_btn)
        toolbar_row.addWidget(delete_row_btn)
        toolbar_row.addWidget(add_column_btn)
        toolbar_row.addWidget(save_csv_btn)
        toolbar_row.addWidget(reload_csv_btn)
        toolbar_row.addWidget(self.manager_count_label)

        self.table_view = QTableView()
        self.table_view.setModel(self.manager_proxy)
        self.table_view.setAlternatingRowColors(True)
        self.table_view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table_view.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table_view.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.EditKeyPressed
            | QAbstractItemView.EditTrigger.SelectedClicked
        )
        self.table_view.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.table_view.horizontalHeader().setStretchLastSection(True)
        self.table_view.verticalHeader().setVisible(True)

        root.addLayout(toolbar_row)
        root.addWidget(self.table_view, 1)

        add_row_btn.clicked.connect(self.add_row)
        delete_row_btn.clicked.connect(self.delete_selected_rows)
        add_column_btn.clicked.connect(self.add_column)
        save_csv_btn.clicked.connect(self.save_csv)
        reload_csv_btn.clicked.connect(self.reload_csv)

        return tab

    def _connect_signals(self) -> None:
        self.model.dirtyChanged.connect(lambda _: self._update_window_title())
        self.model.modelReset.connect(self.refresh_device_combo)
        self.model.modelReset.connect(self._update_row_counts)
        self.model.modelReset.connect(self._update_selector_columns)
        self.model.rowsInserted.connect(lambda *_: self._update_after_model_change())
        self.model.rowsRemoved.connect(lambda *_: self._update_after_model_change())
        self.model.columnsInserted.connect(lambda *_: self._update_after_model_change())
        self.model.dataChanged.connect(lambda *_: self._update_after_model_change())

        self.device_combo.currentTextChanged.connect(self.on_device_combo_changed)
        self.device_type_combo.currentTextChanged.connect(self.update_device_details)
        self.mode_combo.currentIndexChanged.connect(self._update_mode_ui)
        self.mode_combo.currentIndexChanged.connect(self.update_device_details)

        self.search_edit.textChanged.connect(self.on_manager_search_changed)
        self.selector_search_edit.textChanged.connect(self.on_selector_search_changed)

        self.run_button.clicked.connect(self.start_run)
        self.open_button.clicked.connect(self.start_open_web_ui)
        self.stop_button.clicked.connect(self.stop_run)

        self.process.readyReadStandardOutput.connect(self.on_stdout)
        self.process.readyReadStandardError.connect(self.on_stderr)
        self.process.finished.connect(self.on_process_finished)
        self.process.errorOccurred.connect(self.on_process_error)

        self.table_view.selectionModel().selectionChanged.connect(lambda *_: self.sync_combo_from_table())
        self.selector_table.selectionModel().selectionChanged.connect(lambda *_: self.sync_combo_from_selector())

        self.script_edit.editingFinished.connect(self.on_script_path_edited)

    def _load_settings(self) -> None:
        self.script_edit.setText(self.settings.value("script_path", ""))

        self.use_device_ip_check.setChecked(self.settings.value("use_device_ip", False, type=bool))
        self.rdp_check.setChecked(self.settings.value("rdp", False, type=bool))

        geometry = self.settings.value("geometry")
        if geometry is not None:
            self.restoreGeometry(geometry)

        self._fill_default_paths_if_blank()
        self._refresh_script_metadata()

        if self.device_list_edit.text().strip():
            try:
                self.reload_csv(silent=True)
            except Exception:
                pass

        saved_mode = self.settings.value("selected_mode", "configure")
        saved_mode_index = self.mode_combo.findData(saved_mode)
        if saved_mode_index >= 0:
            self.mode_combo.setCurrentIndex(saved_mode_index)

        saved_device = self.settings.value("selected_device", "")
        if saved_device:
            self.device_combo.setCurrentText(saved_device)

        saved_type_text = self.settings.value("selected_type_text", "")
        if saved_type_text:
            self.device_type_combo.setCurrentText(saved_type_text)

        self._update_mode_ui()

    def _fill_default_paths_if_blank(self) -> None:
        if not self.script_edit.text().strip():
            self.script_edit.setText(str(BASE_DIR / DEFAULT_SCRIPT_NAME))

    def _refresh_script_metadata(self) -> None:
        script_text = self.script_edit.text().strip() or str(BASE_DIR / DEFAULT_SCRIPT_NAME)
        script_path = clean_path(script_text)
        script_dir = script_dir_from_path(script_path)

        self.metadata = read_cortex_script_metadata(script_path)
        self.model.set_valid_device_types(self.metadata.valid_panel_types)

        self.device_list_edit.setText(str(script_dir / DEFAULT_DEVICE_LIST_NAME))
        self.template_dir_edit.setText(str(script_dir))
        self.adapter_edit.setText(self.metadata.adapter_name)
        self.default_ip_edit.setText(self.metadata.default_device_ip)
        self.default_ip_value.setText(self.metadata.default_device_ip)

        self._refresh_device_type_combo()
        self.valid_types_edit.setText(", ".join(self.metadata.valid_panel_types))
        self.update_device_details()

    def _refresh_device_type_combo(self) -> None:
        current = self.device_type_combo.currentText().strip()

        self.device_type_combo.blockSignals(True)
        self.device_type_combo.clear()
        self.device_type_combo.addItem("")
        for value in self.metadata.valid_panel_types:
            self.device_type_combo.addItem(value)

        if current:
            self.device_type_combo.setCurrentText(current)
        self.device_type_combo.blockSignals(False)

    def use_gui_folder_defaults(self) -> None:
        if not self.maybe_save_csv():
            return

        self.script_edit.setText(str(BASE_DIR / DEFAULT_SCRIPT_NAME))
        self._refresh_script_metadata()
        self.reload_csv(silent=True)
        self.update_device_details()
        self.statusBar().showMessage("Applied GUI folder defaults.", 4000)

    def save_settings_now(self) -> None:
        self._save_settings()
        self.statusBar().showMessage("Settings saved.", 3000)

    def _save_settings(self) -> None:
        self.settings.setValue("script_path", self.script_edit.text().strip())
        self.settings.setValue("use_device_ip", self.use_device_ip_check.isChecked())
        self.settings.setValue("rdp", self.rdp_check.isChecked())
        self.settings.setValue("selected_device", self.device_combo.currentText().strip())
        self.settings.setValue("selected_type_text", self.device_type_combo.currentText().strip())
        self.settings.setValue("selected_mode", self._current_mode_key())
        self.settings.setValue("geometry", self.saveGeometry())

    def _update_window_title(self) -> None:
        suffix = " *" if self.model.dirty else ""
        self.setWindowTitle(f"Cortex Configurator{suffix}")

    def _update_after_model_change(self) -> None:
        self.refresh_device_combo()
        self.update_device_details()
        self._update_row_counts()
        self._update_window_title()
        self._update_selector_columns()
        self._select_current_device_in_selector()

    def _update_row_counts(self) -> None:
        row_count = self.model.rowCount()
        device_count = len(self.model.device_names())
        self.manager_count_label.setText(f"{row_count} rows")
        self.device_count_label.setText(f"{device_count} devices loaded")
        self.selector_count_label.setText(f"{self.selector_proxy.rowCount()} visible / {device_count} total")

    def _set_run_state(self, running: bool) -> None:
        self.run_button.setEnabled(not running)
        self.open_button.setEnabled(not running)
        self.stop_button.setEnabled(running)

    def _update_selector_columns(self) -> None:
        preferred = {"device_name", "device_type", "ip_address", "gateway"}

        for column, header in enumerate(self.model.headers):
            hidden = header not in preferred
            self.selector_table.setColumnHidden(column, hidden)

            if not hidden:
                if header == "device_name":
                    self.selector_table.horizontalHeader().setSectionResizeMode(
                        column, QHeaderView.ResizeMode.Stretch
                    )
                else:
                    self.selector_table.horizontalHeader().setSectionResizeMode(
                        column, QHeaderView.ResizeMode.ResizeToContents
                    )

        if self.model.columnCount() > 0:
            self.selector_table.sortByColumn(0, Qt.SortOrder.AscendingOrder)

    def append_log(self, text: str) -> None:
        if not text:
            return
        self.log_output.insertPlainText(text.replace("\r\n", "\n"))
        self.log_output.ensureCursorVisible()

    def on_script_path_edited(self) -> None:
        self._refresh_script_metadata()
        self.reload_csv(silent=True)
        self.statusBar().showMessage("Script path updated.", 3000)

    def browse_script(self) -> None:
        filename, _ = QFileDialog.getOpenFileName(
            self,
            "Select Cortex Script",
            self.script_edit.text().strip() or str(BASE_DIR / DEFAULT_SCRIPT_NAME),
            "Python Files (*.py);;All Files (*)",
        )
        if not filename:
            return

        self.script_edit.setText(filename)
        self._refresh_script_metadata()
        self.reload_csv(silent=True)
        self.statusBar().showMessage("Script selected.", 3000)

    def reload_csv(self, silent: bool = False) -> bool:
        if not silent and not self.maybe_save_csv():
            return False

        path_text = self.device_list_edit.text().strip()
        if not path_text:
            QMessageBox.warning(self, "CSV Required", "Please choose a valid Cortex script first.")
            return False

        try:
            path = clean_path(path_text)
            self.model.load_csv(path)
            self.refresh_device_combo()
            self.update_device_details()
            self._update_row_counts()
            self._update_selector_columns()

            if not silent:
                if path.exists():
                    self.statusBar().showMessage(f"Loaded CSV: {path}", 4000)
                else:
                    self.statusBar().showMessage(
                        "CSV file does not exist yet. Blank table loaded; click Save CSV to create it.",
                        5000,
                    )
            return True
        except Exception as exc:
            QMessageBox.critical(self, "Load CSV Failed", str(exc))
            return False

    def save_csv(self) -> bool:
        errors = self.model.validate_rows()
        if errors:
            preview = "\n".join(errors[:12])
            more = "" if len(errors) <= 12 else f"\n...and {len(errors) - 12} more"
            QMessageBox.warning(
                self,
                "CSV Validation Failed",
                f"Please fix these issues before saving:\n\n{preview}{more}",
            )
            return False

        path_text = self.device_list_edit.text().strip()
        if not path_text:
            QMessageBox.warning(self, "Save CSV", "Select a valid Cortex script first.")
            return False

        try:
            self.model.save_csv(clean_path(path_text))
            self.statusBar().showMessage("CSV saved.", 3000)
            self.refresh_device_combo()
            self._update_selector_columns()
            return True
        except Exception as exc:
            QMessageBox.critical(self, "Save CSV Failed", str(exc))
            return False

    def maybe_save_csv(self) -> bool:
        if not self.model.dirty:
            return True

        result = QMessageBox.question(
            self,
            "Unsaved Changes",
            "deviceList.csv has unsaved changes. Save before continuing?",
            QMessageBox.StandardButton.Yes
            | QMessageBox.StandardButton.No
            | QMessageBox.StandardButton.Cancel,
        )

        if result == QMessageBox.StandardButton.Cancel:
            return False
        if result == QMessageBox.StandardButton.Yes:
            return self.save_csv()
        return True

    def add_row(self) -> None:
        insert_at = self.model.rowCount()
        self.model.insertRows(insert_at, 1)
        source_index = self.model.index(insert_at, 0)
        proxy_index = self.manager_proxy.mapFromSource(source_index)

        if not proxy_index.isValid():
            self.search_edit.clear()
            proxy_index = self.manager_proxy.mapFromSource(source_index)

        if proxy_index.isValid():
            self.table_view.selectRow(proxy_index.row())
            self.table_view.scrollTo(proxy_index)
            self.table_view.edit(proxy_index)

    def delete_selected_rows(self) -> None:
        selected = self.table_view.selectionModel().selectedRows()
        if not selected:
            return

        source_rows = sorted({self.manager_proxy.mapToSource(index).row() for index in selected}, reverse=True)
        for row in source_rows:
            self.model.removeRows(row, 1)

    def add_column(self) -> None:
        name, ok = QInputDialog.getText(self, "Add Column", "New column name:")
        if not ok:
            return

        name = name.strip()
        if not name:
            return

        if not self.model.add_column(name):
            QMessageBox.warning(self, "Add Column", f"Could not add column '{name}'.")
            return

        self.statusBar().showMessage(f"Added column: {name}", 3000)

    def on_manager_search_changed(self, text: str) -> None:
        self.manager_proxy.setFilterText(text)

    def on_selector_search_changed(self, text: str) -> None:
        self.selector_proxy.setFilterText(text)
        self._update_row_counts()
        self._select_current_device_in_selector()

    def refresh_device_combo(self) -> None:
        current = self.device_combo.currentText().strip()
        names = self.model.device_names()
        mode = self._current_mode_key()

        self.device_combo.blockSignals(True)
        self.device_combo.clear()
        self.device_combo.addItems(names)

        if current and current in names:
            self.device_combo.setCurrentText(current)
        elif should_keep_typed_prefix(mode, current, names):
            self.device_combo.setEditText(current)
        elif names:
            if is_prefix_mode(mode):
                self.device_combo.setEditText(current or names[0])
            else:
                self.device_combo.setCurrentIndex(0)
        self.device_combo.blockSignals(False)

        self.update_device_details()
        self._update_row_counts()
        self._select_current_device_in_selector()

    def sync_combo_from_table(self) -> None:
        selected = self.table_view.selectionModel().selectedRows()
        if not selected:
            return

        source_index = self.manager_proxy.mapToSource(selected[0])
        row = self.model.rows[source_index.row()]
        name = row.get("device_name", "").strip()
        if name:
            self.device_combo.setCurrentText(name)

    def sync_combo_from_selector(self) -> None:
        if self._syncing_device_selection:
            return

        selected = self.selector_table.selectionModel().selectedRows()
        if not selected:
            return

        source_index = self.selector_proxy.mapToSource(selected[0])
        row = self.model.rows[source_index.row()]
        name = row.get("device_name", "").strip()
        if not name:
            return

        self._syncing_device_selection = True
        try:
            self.device_combo.setCurrentText(name)
        finally:
            self._syncing_device_selection = False

        self.update_device_details()

    def _select_current_device_in_selector(self) -> None:
        if self._syncing_device_selection:
            return

        name = self.device_combo.currentText().strip()
        if not name:
            self.selector_table.clearSelection()
            return

        source_row = self.model.find_device_row_index(name)
        if source_row is None:
            self.selector_table.clearSelection()
            return

        source_index = self.model.index(source_row, 0)
        proxy_index = self.selector_proxy.mapFromSource(source_index)
        if not proxy_index.isValid():
            return

        self._syncing_device_selection = True
        try:
            self.selector_table.selectRow(proxy_index.row())
            self.selector_table.scrollTo(proxy_index, QAbstractItemView.ScrollHint.PositionAtCenter)
        finally:
            self._syncing_device_selection = False

    def on_device_combo_changed(self) -> None:
        self.update_device_details()
        if not self._syncing_device_selection:
            self._select_current_device_in_selector()

    def _current_mode_key(self) -> str:
        return str(self.mode_combo.currentData() or "configure")

    def _current_mode_label(self) -> str:
        index = self.mode_combo.currentIndex()
        return self.mode_combo.itemText(index) if index >= 0 else "Configure Device"

    def _update_mode_ui(self) -> None:
        mode = self._current_mode_key()

        hints = {
            "configure": (
                "Configure one exact device_name from the CSV. "
                "If the selected row has no device_type yet, choose one in 'Type Override / Filter'."
            ),
            "diag": (
                "Diag mode scans known subnets, finds the Cortex device, uploads the corrected config, then restarts it. "
                "Requires one exact device_name."
            ),
            "pingall": (
                "Scans device-list subnets and then the default subnet for Cortex responses. "
                "Requires one exact device_name."
            ),
            "reboot": (
                "Reboots one exact device at its configured IP and verifies it after restart."
            ),
            "verifyall": (
                "Treats the Device field as a name prefix and verifies all matching rows. "
                "Type Override / Filter is optional."
            ),
            "configall": (
                "Treats the Device field as a name prefix and uploads/restarts all matching rows of the selected type. "
                "Type Override / Filter is required. The GUI auto-adds --a2 for this mode."
            ),
        }

        run_text = {
            "configure": "Run Configuration",
            "diag": "Run Diag",
            "pingall": "Run Scan",
            "reboot": "Run Reboot",
            "verifyall": "Run Verify All",
            "configall": "Run Config All",
        }.get(mode, "Run")

        device_placeholder = (
            "Enter a device-name prefix"
            if mode in PREFIX_MODES
            else "Select an exact device_name"
        )
        type_placeholder = {
            "configure": "Optional if CSV type is blank",
            "diag": "Optional if CSV type is blank",
            "pingall": "Optional if CSV type is blank",
            "reboot": "Optional if CSV type is blank",
            "verifyall": "Optional type filter",
            "configall": "Required type filter/template",
        }.get(mode, "Optional")

        self.device_combo.lineEdit().setPlaceholderText(device_placeholder)
        self.device_type_combo.lineEdit().setPlaceholderText(type_placeholder)
        self.mode_hint_label.setText(hints.get(mode, ""))
        self.run_button.setText(run_text)

    def update_device_details(self) -> None:
        row = self.model.find_device(self.device_combo.currentText().strip())
        override_type = self.device_type_combo.currentText().strip()

        row_type = row.get("device_type", "").strip() if row else ""
        resolved_type = row_type or override_type

        if row_type:
            self.type_value.setText(row_type if not override_type or override_type == row_type else f"{row_type} (CSV)")
        else:
            self.type_value.setText(override_type or "-")

        if row:
            ip_address = row.get("ip_address", "").strip()
            self.target_ip_value.setText(ip_address or "-")

            gateway_text = "-"
            if ip_address and is_valid_ipv4(ip_address):
                try:
                    gateway_text = derive_gateway(ip_address)
                except Exception:
                    gateway_text = "-"
            self.gateway_value.setText(gateway_text)
        else:
            self.target_ip_value.setText("-")
            self.gateway_value.setText("-")

        template_dir_text = self.template_dir_edit.text().strip()
        if template_dir_text and resolved_type:
            path = clean_path(template_dir_text) / f"Cortexsettings ({resolved_type}).json"
            exists = "FOUND" if path.exists() else "MISSING"
            self.template_value.setText(f"{path}   [{exists}]")
        else:
            self.template_value.setText("-")

        self.default_ip_value.setText(self.metadata.default_device_ip or "-")

    def build_process_args(self) -> list[str]:
        script_text = self.script_edit.text().strip()
        if not script_text:
            raise ValueError("Script path is required.")

        script_path = clean_path(script_text)
        if not script_path.exists():
            raise ValueError(f"Script not found: {script_path}")

        target_text = self.device_combo.currentText().strip()
        if not target_text:
            raise ValueError("Please select a device_name or enter a prefix.")

        mode = self._current_mode_key()
        valid_types = self.metadata.valid_panel_types or FALLBACK_PANEL_TYPES

        type_text = self.device_type_combo.currentText().strip()
        if type_text and valid_types and type_text not in valid_types:
            raise ValueError(
                f"Invalid device type '{type_text}'. Valid types: {', '.join(valid_types)}"
            )

        device_row = self.model.find_device(target_text)
        args: list[str] = ["-u", str(script_path), target_text]

        if is_prefix_mode(mode):
            if mode == "verifyall":
                if type_text:
                    args.append(type_text)
                args.append("--verifyall")
            elif mode == "configall":
                if not type_text:
                    raise ValueError("Configure by Prefix requires a device type.")
                args.append(type_text)
                args.append("--configall")
            else:
                raise ValueError(f"Unsupported prefix mode: {mode}")

        elif is_single_device_mode(mode):
            if device_row is None:
                raise ValueError(
                    f"{self._current_mode_label()} requires an exact device_name from deviceList.csv."
                )

            row_type = device_row.get("device_type", "").strip()

            if mode in SINGLE_DEVICE_MODES_REQUIRING_TYPE:
                if row_type and type_text and type_text != row_type:
                    raise ValueError(
                        "This device already has a device_type in deviceList.csv. "
                        "upload_cortex.py only uses the optional positional device_type when the CSV value is blank. "
                        "Edit the CSV row or clear the override."
                    )

                effective_type = row_type or type_text
                if not effective_type:
                    raise ValueError(
                        "This mode requires a device_type. "
                        "The selected device has a blank device_type in deviceList.csv, so choose one in the GUI first."
                    )

                if not row_type and type_text:
                    args.append(type_text)

            if mode == "diag":
                args.append("--diag")
            elif mode == "pingall":
                args.append("--pingall")
            elif mode == "reboot":
                args.append("--reboot")

        else:
            raise ValueError(f"Unsupported mode: {mode}")

        if self.use_device_ip_check.isChecked() or mode == "configall":
            args.append("--a2")

        if self.rdp_check.isChecked():
            args.append("--rdp")

        return args

    def build_open_args(self) -> list[str]:
        script_text = self.script_edit.text().strip()
        if not script_text:
            raise ValueError("Script path is required.")

        script_path = clean_path(script_text)
        if not script_path.exists():
            raise ValueError(f"Script not found: {script_path}")

        device_name = self.device_combo.currentText().strip()
        if not device_name:
            raise ValueError("Please select a device_name.")

        device_row = self.model.find_device(device_name)
        if device_row is None:
            raise ValueError("Open Web UI requires an exact device_name from deviceList.csv.")

        return ["-u", str(script_path), device_name, "--open"]

    def start_run(self) -> None:
        if self.process.state() != QProcess.ProcessState.NotRunning:
            QMessageBox.information(self, "Already Running", "A configuration run is already in progress.")
            return

        if not self.maybe_save_csv():
            return

        if not self.save_csv():
            return

        try:
            args = self.build_process_args()
            script_path = clean_path(self.script_edit.text().strip())

            self.log_output.clear()
            self.append_log("=== Starting Cortex configurator ===\n")
            self.append_log(join_command([sys.executable, *args]) + "\n\n")

            self.process.setWorkingDirectory(str(script_path.parent))
            self.process.setProcessEnvironment(QProcessEnvironment.systemEnvironment())
            self.process.setProgram(sys.executable)
            self.process.setArguments(args)
            self.process.start()

            self._set_run_state(True)
            self._save_settings()
            self.statusBar().showMessage("Run started...")
        except Exception as exc:
            QMessageBox.critical(self, "Cannot Start", str(exc))
            self._set_run_state(False)

    def start_open_web_ui(self) -> None:
        if self.process.state() != QProcess.ProcessState.NotRunning:
            QMessageBox.information(self, "Already Running", "A configuration run is already in progress.")
            return

        if not self.maybe_save_csv():
            return

        try:
            args = self.build_open_args()
            script_path = clean_path(self.script_edit.text().strip())

            self.log_output.clear()
            self.append_log("=== Opening Cortex Web UI ===\n")
            self.append_log(join_command([sys.executable, *args]) + "\n\n")

            self.process.setWorkingDirectory(str(script_path.parent))
            self.process.setProcessEnvironment(QProcessEnvironment.systemEnvironment())
            self.process.setProgram(sys.executable)
            self.process.setArguments(args)
            self.process.start()

            self._set_run_state(True)
            self._save_settings()
            self.statusBar().showMessage("Open Web UI command started...")
        except Exception as exc:
            QMessageBox.critical(self, "Cannot Open Web UI", str(exc))
            self._set_run_state(False)

    def stop_run(self) -> None:
        if self.process.state() == QProcess.ProcessState.NotRunning:
            return

        self.append_log("\n=== Stop requested ===\n")
        self.process.terminate()

        def force_kill_if_needed() -> None:
            if self.process.state() != QProcess.ProcessState.NotRunning:
                self.append_log("Process did not stop gracefully; killing it.\n")
                self.process.kill()

        QTimer.singleShot(3000, force_kill_if_needed)

    def on_stdout(self) -> None:
        data = bytes(self.process.readAllStandardOutput()).decode(APP_ENCODING, errors="replace")
        self.append_log(data)

    def on_stderr(self) -> None:
        data = bytes(self.process.readAllStandardError()).decode(APP_ENCODING, errors="replace")
        self.append_log(data)

    def on_process_error(self, _error: QProcess.ProcessError) -> None:
        self.append_log(f"\n[Process Error] {self.process.errorString()}\n")
        self.statusBar().showMessage("Process error.", 5000)
        self._set_run_state(False)

    def on_process_finished(self, exit_code: int, exit_status: QProcess.ExitStatus) -> None:
        self._set_run_state(False)

        if exit_status == QProcess.ExitStatus.NormalExit and exit_code == 0:
            self.append_log("\n=== Completed successfully ===\n")
            self.statusBar().showMessage("Command completed successfully.", 5000)
        elif exit_code == 130:
            self.append_log("\n=== Interrupted ===\n")
            self.statusBar().showMessage("Run interrupted.", 5000)
        else:
            self.append_log(f"\n=== Finished with exit code {exit_code} ===\n")
            self.statusBar().showMessage(f"Run finished with exit code {exit_code}.", 5000)

    def closeEvent(self, event) -> None:
        if self.process.state() != QProcess.ProcessState.NotRunning:
            result = QMessageBox.question(
                self,
                "Close Application",
                "A configuration run is still active. Stop it and close?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if result != QMessageBox.StandardButton.Yes:
                event.ignore()
                return

            self.process.kill()
            self.process.waitForFinished(2000)

        if not self.maybe_save_csv():
            event.ignore()
            return

        self._save_settings()
        event.accept()


def apply_dark_theme(app: QApplication) -> None:
    app.setStyle("Fusion")

    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor("#12161d"))
    palette.setColor(QPalette.ColorRole.WindowText, QColor("#edf2f7"))
    palette.setColor(QPalette.ColorRole.Base, QColor("#0f141b"))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor("#151c25"))
    palette.setColor(QPalette.ColorRole.ToolTipBase, QColor("#0f141b"))
    palette.setColor(QPalette.ColorRole.ToolTipText, QColor("#edf2f7"))
    palette.setColor(QPalette.ColorRole.Text, QColor("#edf2f7"))
    palette.setColor(QPalette.ColorRole.Button, QColor("#1a2230"))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor("#edf2f7"))
    palette.setColor(QPalette.ColorRole.BrightText, QColor("#ffffff"))
    palette.setColor(QPalette.ColorRole.Highlight, QColor("#2f80ed"))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#ffffff"))
    app.setPalette(palette)

    app.setStyleSheet(
        """
        QWidget {
            font-family: "Segoe UI", Arial, sans-serif;
            font-size: 10pt;
        }
        QFrame#heroCard {
            background: qlineargradient(
                x1: 0, y1: 0, x2: 1, y2: 1,
                stop: 0 #17212d,
                stop: 1 #111923
            );
            border: 1px solid #273041;
            border-radius: 14px;
        }
        QLabel#titleLabel {
            font-size: 20pt;
            font-weight: 700;
            color: #f7fbff;
        }
        QLabel#subtitleLabel {
            color: #9db0c5;
        }
        QGroupBox {
            border: 1px solid #273041;
            border-radius: 12px;
            margin-top: 14px;
            padding: 12px;
            font-weight: 600;
        }
        QGroupBox::title {
            subcontrol-origin: margin;
            left: 12px;
            padding: 0 4px;
            color: #9db0c5;
        }
        QLineEdit, QComboBox, QPlainTextEdit, QTableView {
            background-color: #0f141b;
            border: 1px solid #2a3445;
            border-radius: 8px;
            padding: 6px 8px;
            selection-background-color: #2f80ed;
        }
        QPushButton {
            background-color: #1a2230;
            border: 1px solid #2a3445;
            border-radius: 8px;
            padding: 8px 14px;
        }
        QPushButton:hover {
            background-color: #222d3d;
        }
        QPushButton#primaryButton {
            background-color: #2f80ed;
            border: 1px solid #2f80ed;
            color: white;
            font-weight: 700;
        }
        QPushButton#dangerButton {
            background-color: #8f2d32;
            border: 1px solid #b14349;
            color: white;
            font-weight: 700;
        }
        QHeaderView::section {
            background-color: #1a2230;
            color: #d9e3ee;
            border: none;
            border-right: 1px solid #273041;
            border-bottom: 1px solid #273041;
            padding: 6px;
        }
        """
    )


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("Cortex Configurator")
    app.setOrganizationName("LocalAutomation")

    apply_dark_theme(app)

    window = MainWindow()
    window.show()

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())