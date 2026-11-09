from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QDialog, QDialogButtonBox, QHeaderView, QLabel, QTreeWidget, QTreeWidgetItem, QVBoxLayout

from .document import ACTIVE_NODE, LAYER_TYPES, VISIBLE_COMPOSITE, source_limitation


class SourcePicker(QDialog):
    def __init__(self, document, selected, parent):
        super().__init__(parent)
        self.setWindowTitle("Choose source")
        self.resize(530, 400)
        layout = QVBoxLayout(self)
        prompt = QLabel("Choose a layer, a group composite, or the visible composite of the document.")
        prompt.setWordWrap(True)
        layout.addWidget(prompt)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(("Source", "Type"))
        self.tree.header().setSectionResizeMode(0, QHeaderView.Stretch)
        self.tree.header().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        layout.addWidget(self.tree)

        visible = QTreeWidgetItem(self.tree, ("Visible composite", "Document"))
        visible.setData(0, Qt.UserRole, VISIBLE_COMPOSITE)
        active = QTreeWidgetItem(self.tree, ("Active layer or group", "Krita selection"))
        active.setData(0, Qt.UserRole, ACTIVE_NODE)
        active.setToolTip(0, "Read the layer or group selected in Krita’s Layers docker when you capture.")
        current = self.add_layers(self.tree.invisibleRootItem(), document.rootNode(), selected)
        self.tree.setCurrentItem(active if selected == ACTIVE_NODE else current or visible)
        self.tree.scrollToItem(self.tree.currentItem())

        actions = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.capture_button = actions.button(QDialogButtonBox.Ok)
        self.capture_button.setText("Capture source")
        self.tree.currentItemChanged.connect(self.update_capture_button)
        self.tree.itemDoubleClicked.connect(self.capture_selected)
        self.update_capture_button()
        actions.accepted.connect(self.accept)
        actions.rejected.connect(self.reject)
        layout.addWidget(actions)

    def add_layers(self, parent, container, selected):
        current = None
        for node in reversed(container.childNodes()):
            kind = node.type()
            if kind not in LAYER_TYPES:
                continue
            limitation = source_limitation(node, kind)
            label = "Pass-through group" if kind == "grouplayer" and limitation else LAYER_TYPES[kind]
            item = QTreeWidgetItem(parent, (node.name(), label))
            item.setIcon(0, node.icon())
            if limitation:
                item.setFlags(item.flags() & ~Qt.ItemIsSelectable)
                item.setToolTip(0, limitation)
                item.setToolTip(1, limitation)
            else:
                node_id = node.uniqueId().toString()
                item.setData(0, Qt.UserRole, node_id)
                if node_id == selected:
                    current = item
            if kind == "grouplayer":
                child = self.add_layers(item, node, selected)
                if child is not None:
                    item.setExpanded(True)
                    current = child
        return current

    def choice(self):
        item = self.tree.currentItem()
        return item.data(0, Qt.UserRole) if item is not None else None

    def update_capture_button(self):
        self.capture_button.setEnabled(self.choice() is not None)

    def capture_selected(self, item):
        if item.data(0, Qt.UserRole) is not None:
            self.accept()
