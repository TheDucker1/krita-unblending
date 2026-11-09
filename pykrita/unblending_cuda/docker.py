from colorsys import hsv_to_rgb, rgb_to_hsv
from dataclasses import replace
from math import exp, log

from krita import DockWidget, Krita
from PyQt5.QtCore import QCoreApplication, QRectF, QSignalBlocker, QSize, QThread, Qt, pyqtSignal
from PyQt5.QtGui import QColor, QIcon, QPainter, QPixmap
from PyQt5.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QFileDialog, QFormLayout, QHBoxLayout,
    QLabel, QListWidget, QListWidgetItem, QPushButton, QScrollArea, QSizePolicy,
    QSlider, QSpinBox, QStackedWidget, QVBoxLayout, QWidget,
)

from .color_widgets import ColorWheel, ImagePreview, VarianceHex, bgra_bytes, bgra_image, draw_checker
from .document import ACTIVE_NODE, VISIBLE_COMPOSITE, capture, insert_result
from .sdk import BACKENDS, BLEND_LABELS, MODES, LayerSpec, UnblendingSdk, read_preset, spread, write_preset
from .source_picker import SourcePicker


PRESET_FILTER = "Layer settings (*.json);;All files (*)"
STARTERS = (
    LayerSpec("Background", (0.18, 0.22, 0.35), 0.045),
    LayerSpec("Color", (0.81, 0.49, 0.26), 0.035),
)


def title(text):
    label = QLabel(text)
    font = label.font()
    font.setBold(True)
    label.setFont(font)
    return label


def button(text, action, tip=""):
    widget = QPushButton(text)
    widget.setToolTip(tip)
    widget.clicked.connect(action)
    return widget


def combo(items):
    widget = QComboBox()
    for label, value in items:
        widget.addItem(label, value)
    return widget


def layer_icon(spec, image):
    tile = QPixmap(38, 28)
    tile.fill(Qt.transparent)
    painter = QPainter(tile)
    painter.fillRect(QRectF(0, 0, 38, 28), QColor.fromRgbF(*spec.mean))
    if image is not None:
        box = QRectF(8, 0, 30, 28)
        draw_checker(painter, box, 4)
        thumb = image.scaled(30, 28, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        painter.drawImage(QRectF(
            box.center().x() - thumb.width() / 2, box.center().y() - thumb.height() / 2,
            thumb.width(), thumb.height(),
        ), thumb)
    painter.end()
    return QIcon(tile)


class SolveJob(QThread):
    progress = pyqtSignal(str)
    solved = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, image, depth, specs, width, backend, opaque, refine, smooth):
        super().__init__(QCoreApplication.instance())
        self.image = image
        self.depth = depth
        self.specs = specs
        self.width = width
        self.options = backend, opaque, refine, smooth
        # Keep a live native call joined if Krita exits or the dock's window closes.
        app = QCoreApplication.instance()
        app.aboutToQuit.connect(self.join)
        self.finished.connect(self.deleteLater)

    def join(self):
        self.wait()

    def run(self):
        try:
            image = self.image
            if self.width != image.width():
                height = max(1, round(self.width * image.height() / image.width()))
                if self.width * height > 2_147_483_647:
                    raise ValueError("The requested solve dimensions are too large.")
                image = image.scaled(self.width, height, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
            if image.isNull():
                raise MemoryError("Could not allocate an image at the requested solve width.")
            width, height = image.width(), image.height()
            data = UnblendingSdk().solve(
                bgra_bytes(image, self.depth), width, height, self.depth,
                self.specs, *self.options, self.progress.emit
            )
            images = tuple(bgra_image(layer, width, height, self.depth) for layer in data)
            if any(image.isNull() for image in images):
                raise MemoryError("Could not allocate the layer previews.")
            self.solved.emit(images)
        except Exception as exc:
            self.failed.emit(str(exc) or exc.__class__.__name__)


class UnblendingDocker(DockWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Unblending")
        self.source = None
        self.source_choice = VISIBLE_COMPOSITE
        self.source_document = None
        self.result = ()
        self.job = None
        self.hue = 0.0
        self.saturation = 0.0

        shell = QWidget()
        outer = QVBoxLayout(shell)
        outer.setContentsMargins(6, 6, 6, 6)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        outer.addWidget(scroll, 1)
        self.controls = QWidget()
        scroll.setWidget(self.controls)
        content = QVBoxLayout(self.controls)
        content.setContentsMargins(4, 2, 4, 2)
        self.build_source(content)
        self.build_layers(content)
        self.build_editor(content)
        self.build_options(content)
        content.addStretch()

        self.size_controls = QWidget()
        self.build_solve_width(self.size_controls)
        outer.addWidget(self.size_controls)
        actions = QHBoxLayout()
        self.solve_button = button("Solve", self.solve)
        self.solve_button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.apply_button = button("Apply as layers", self.apply_result)
        actions.addWidget(self.solve_button, 1)
        actions.addWidget(self.apply_button, 1)
        outer.addLayout(actions)
        self.status = QLabel("Capture an image from the current document to get started.")
        self.status.setWordWrap(True)
        outer.addWidget(self.status)
        self.setWidget(shell)

        self.set_layers(STARTERS)
        self.update_actions()

    def build_source(self, content):
        content.addWidget(title("Source & preview"))
        controls = QHBoxLayout()
        self.choose_source_button = button("Choose source…", self.choose_source)
        controls.addWidget(self.choose_source_button, 1)
        self.capture_button = button("Capture", self.capture_source, "Refresh the selected source from Krita.")
        controls.addWidget(self.capture_button)
        content.addLayout(controls)
        previews = QHBoxLayout()
        self.input_view = ImagePreview("Capture from Krita", sample=True)
        self.input_view.picked.connect(self.pick_color)
        self.output_view = ImagePreview("Solve to preview")
        for label, view in (("Source · click to pick", self.input_view), ("Selected layer", self.output_view)):
            column = QVBoxLayout()
            column.addWidget(QLabel(label))
            column.addWidget(view, 1)
            previews.addLayout(column, 1)
        content.addLayout(previews)
        self.source_label = QLabel("No source captured")
        self.source_label.setTextFormat(Qt.PlainText)
        self.source_label.setWordWrap(True)
        content.addWidget(self.source_label)

    def build_layers(self, content):
        bar = QHBoxLayout()
        bar.addWidget(title("Layer stack"), 1)
        bar.addWidget(button("Open…", self.open_preset, "Load LayerInfos from a JSON file."))
        bar.addWidget(button("Save…", self.save_preset, "Save LayerInfos to a JSON file."))
        content.addLayout(bar)
        self.layers = QListWidget()
        self.layers.setDragDropMode(QAbstractItemView.InternalMove)
        self.layers.setDefaultDropAction(Qt.MoveAction)
        self.layers.setEditTriggers(QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed)
        self.layers.setIconSize(QSize(38, 28))
        self.layers.setToolTip("Top row is the front layer. Drag to reorder. Double-click to rename.")
        self.layers.currentItemChanged.connect(self.select_layer)
        self.layers.itemChanged.connect(self.rename_layer)
        self.layers.model().rowsMoved.connect(self.layers_moved)
        content.addWidget(self.layers)
        tools = QHBoxLayout()
        tools.addWidget(button("+ Layer", self.add_layer, "Insert a layer above the selected layer."))
        self.remove_button = button("Remove", self.remove_layer, "Remove the selected layer.")
        self.up_button = button("↑", lambda: self.move_layer(-1), "Move selected layer up.")
        self.down_button = button("↓", lambda: self.move_layer(1), "Move selected layer down.")
        self.up_button.setFixedWidth(32)
        self.down_button.setFixedWidth(32)
        for control in (self.remove_button, self.up_button, self.down_button):
            tools.addWidget(control)
        tools.addStretch()
        content.addLayout(tools)

    def build_editor(self, content):
        content.addWidget(title("Selected layer color"))
        colors = QHBoxLayout()
        wheel_column = QVBoxLayout()
        wheel_column.addWidget(QLabel("Pick base color"), alignment=Qt.AlignHCenter)
        self.wheel = ColorWheel()
        self.wheel.picked.connect(self.pick_hue)
        wheel_column.addWidget(self.wheel, alignment=Qt.AlignHCenter)
        self.brightness = QSlider(Qt.Horizontal)
        self.brightness.setRange(0, 1000)
        self.brightness.setAccessibleName("Brightness")
        self.brightness.valueChanged.connect(self.change_brightness)
        wheel_column.addWidget(QLabel("Brightness"))
        wheel_column.addWidget(self.brightness)
        colors.addLayout(wheel_column, 1)

        hex_column = QVBoxLayout()
        hex_column.addWidget(QLabel("Color spread"), alignment=Qt.AlignHCenter)
        self.hexagon = VarianceHex()
        hex_column.addWidget(self.hexagon, alignment=Qt.AlignHCenter)
        self.spread_label = QLabel()
        hex_column.addWidget(self.spread_label)
        self.spread_slider = QSlider(Qt.Horizontal)
        self.spread_slider.setRange(0, 1000)
        self.spread_slider.setAccessibleName("Color spread")
        self.spread_slider.valueChanged.connect(self.change_spread)
        hex_column.addWidget(self.spread_slider)
        colors.addLayout(hex_column, 1)
        content.addLayout(colors)

        settings = QFormLayout()
        self.blend = combo((BLEND_LABELS.get(name, name), name) for name in MODES)
        self.blend.currentIndexChanged.connect(lambda: self.edit_layer(mode=self.blend.currentData()))
        settings.addRow("Blend mode", self.blend)
        self.composition = combo((("Source over", "source-over"), ("Plus (preview only)", "plus")))
        self.composition.currentIndexChanged.connect(
            lambda: self.edit_layer(composition=self.composition.currentData())
        )
        settings.addRow("Composition", self.composition)
        content.addLayout(settings)

    def build_solve_width(self, widget):
        row = QHBoxLayout(widget)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(QLabel("Solve width"))
        self.width = QSpinBox()
        self.width.setRange(1, 2_147_483_647)
        self.width.setValue(512)
        self.width.setSuffix(" px")
        self.width.valueChanged.connect(self.invalidate_result)
        self.original = button("Original size", self.change_size, "Use the full source dimensions.")
        self.original.setCheckable(True)
        self.original_dimensions = QLabel("Source dimensions")
        self.width_display = QStackedWidget()
        self.width_display.addWidget(self.width)
        self.width_display.addWidget(self.original_dimensions)
        row.addWidget(self.width_display, 1)
        row.addWidget(self.original)

    def build_options(self, content):
        content.addWidget(title("Solver options"))
        form = QFormLayout()
        self.backend = combo(BACKENDS.items())
        self.backend.currentIndexChanged.connect(self.invalidate_result)
        form.addRow("Backend", self.backend)
        self.opaque = QCheckBox("Opaque base layer")
        self.opaque.setChecked(True)
        self.refine = QCheckBox("Refine mattes")
        self.refine.setChecked(True)
        self.refine.toggled.connect(self.update_smooth)
        self.smooth = QCheckBox("Smooth background")
        self.smooth.setToolTip("Requires refinement and an opaque base layer.")
        self.opaque.toggled.connect(self.update_smooth)
        for option in (self.opaque, self.refine, self.smooth):
            option.toggled.connect(self.invalidate_result)
            form.addRow(option)
        content.addLayout(form)

    def canvasChanged(self, canvas):
        document = self.active_document()
        self.check_source_document(document)
        if self.source is not None and not self.source.matches(document):
            self.clear_source()
            if self.job is None:
                self.status.setText("Capture from the current document.")
        self.update_actions()

    @staticmethod
    def active_document():
        return Krita.instance().activeDocument()

    def clear_source(self):
        self.source = None
        self.input_view.set_image()
        self.source_label.setText("No source captured")
        self.original_dimensions.setText("Source dimensions")
        self.invalidate_result()

    def check_source_document(self, document):
        if self.source_document is not None and (document is None or self.source_document != document):
            self.source_choice = VISIBLE_COMPOSITE
            self.source_document = None

    def choose_source(self):
        document = self.active_document()
        if self.job is not None or document is None:
            return
        self.check_source_document(document)
        picker = SourcePicker(document, self.source_choice, self)
        accepted = picker.exec_()
        choice = picker.choice() if accepted else None
        picker.deleteLater()
        current = self.active_document()
        if choice is None or current is None or document != current:
            return
        self.source_choice = choice
        self.source_document = document if choice not in (VISIBLE_COMPOSITE, ACTIVE_NODE) else None
        self.capture_source()

    def capture_source(self):
        if self.job is not None:
            return
        document = self.active_document()
        self.check_source_document(document)
        self.clear_source()
        try:
            self.source = capture(document, self.source_choice)
        except (ValueError, RuntimeError, MemoryError) as exc:
            self.status.setText(str(exc) or "Could not read the source.")
            return
        image = self.source.image
        dimensions = f"{image.width()} × {image.height()}"
        self.original_dimensions.setText(dimensions)
        self.source_label.setText(f"{self.source.label}  ·  {dimensions}")
        self.input_view.set_image(image)
        self.status.setText("Click the source to pick a color, or use the wheel.")
        self.update_actions()

    def change_size(self, original):
        self.width_display.setCurrentWidget(self.original_dimensions if original else self.width)
        self.invalidate_result()

    def update_smooth(self):
        allowed = self.opaque.isChecked() and self.refine.isChecked()
        self.smooth.setEnabled(allowed)
        if not allowed:
            self.smooth.setChecked(False)

    @staticmethod
    def new_item(spec):
        item = QListWidgetItem(layer_icon(spec, None), spec.name)
        item.setFlags((item.flags() | Qt.ItemIsEditable) & ~Qt.ItemIsDropEnabled)
        item.setData(Qt.UserRole, spec)
        return item

    def set_layers(self, specs):
        with QSignalBlocker(self.layers):
            self.layers.clear()
            for spec in reversed(specs):
                self.layers.addItem(self.new_item(spec))
            self.layers.setCurrentRow(0)
        self.select_layer()
        self.invalidate_result()

    def specs(self):
        return tuple(self.layers.item(row).data(Qt.UserRole) for row in reversed(range(self.layers.count())))

    def select_layer(self):
        item = self.layers.currentItem()
        if item is None:
            return
        spec = item.data(Qt.UserRole)
        hue, self.saturation, value = rgb_to_hsv(*spec.mean)
        self.hue = hue
        for control, setting in (
            (self.blend, spec.mode), (self.composition, spec.composition),
        ):
            with QSignalBlocker(control):
                control.setCurrentIndex(control.findData(setting))
        with QSignalBlocker(self.brightness), QSignalBlocker(self.spread_slider):
            self.brightness.setValue(round(value * 1000))
            self.spread_slider.setValue(round(log(spread(spec.variance) / 0.005) * 1000 / log(200)))
        self.refresh_color(spec)
        self.refresh_output()
        self.update_actions()

    def refresh_color(self, spec):
        color = QColor.fromRgbF(*spec.mean)
        self.wheel.set_color(self.hue, self.saturation, color)
        self.hexagon.set_model(spec)
        self.spread_label.setText(f"Spread  σ {spread(spec.variance):.3g}")
        self.wheel.setAccessibleDescription(f"Base color {color.name().upper()}")

    def pick_color(self, color):
        mean = color.getRgbF()[:3]
        hue, self.saturation, value = rgb_to_hsv(*mean)
        if self.saturation:
            self.hue = hue
        with QSignalBlocker(self.brightness):
            self.brightness.setValue(round(value * 1000))
        self.edit_layer(mean=mean)

    def pick_hue(self, hue, saturation):
        self.hue, self.saturation = hue, saturation
        self.change_brightness(self.brightness.value())

    def change_brightness(self, value):
        self.edit_layer(mean=hsv_to_rgb(self.hue, self.saturation, value / 1000))

    def change_spread(self, value):
        item = self.layers.currentItem()
        if item is None:
            return
        sigma = 0.005 * exp(log(200) * value / 1000)
        variance = item.data(Qt.UserRole).variance
        if isinstance(variance, tuple):
            # Scale all axes equally, preserving correlations from a loaded preset.
            old_sigma = spread(variance)
            variance = tuple((entry / old_sigma) / old_sigma * sigma**2 for entry in variance)
        else:
            variance = sigma**2
        self.edit_layer(variance=variance)

    def edit_layer(self, **changes):
        item = self.layers.currentItem()
        if item is None:
            return
        old = item.data(Qt.UserRole)
        spec = replace(old, **changes)
        if spec != old:
            item.setData(Qt.UserRole, spec)
            item.setIcon(layer_icon(spec, None))
            self.invalidate_result()
        self.refresh_color(spec)

    def rename_layer(self, item):
        spec = item.data(Qt.UserRole)
        name = item.text().strip() or spec.name
        if name != item.text():
            item.setText(name)
        if name != spec.name:
            item.setData(Qt.UserRole, replace(spec, name=name))

    def layers_moved(self):
        self.invalidate_result()
        self.select_layer()

    def add_layer(self):
        names = {spec.name for spec in self.specs()}
        number = self.layers.count() + 1
        while f"Layer {number}" in names:
            number += 1
        selected = self.layers.currentItem()
        mean = selected.data(Qt.UserRole).mean if selected else (0.5, 0.5, 0.5)
        row = max(0, self.layers.currentRow())
        self.layers.insertItem(row, self.new_item(LayerSpec(f"Layer {number}", mean)))
        self.layers.setCurrentRow(row)
        self.invalidate_result()

    def remove_layer(self):
        if self.layers.count() > 1:
            self.layers.takeItem(self.layers.currentRow())
            self.invalidate_result()

    def move_layer(self, direction):
        row = self.layers.currentRow()
        target = row + direction
        if 0 <= target < self.layers.count():
            with QSignalBlocker(self.layers):
                item = self.layers.takeItem(row)
                self.layers.insertItem(target, item)
                self.layers.setCurrentItem(item)
            self.layers_moved()

    def open_preset(self):
        path, _ = QFileDialog.getOpenFileName(self, "Open layer settings", "", PRESET_FILTER)
        if path:
            try:
                self.set_layers(read_preset(path))
                self.status.setText("Layer settings loaded.")
            except (OSError, ValueError) as exc:
                self.status.setText(str(exc))

    def save_preset(self):
        path, _ = QFileDialog.getSaveFileName(self, "Save layer settings", "layers.json", PRESET_FILTER)
        if path:
            try:
                write_preset(path, self.specs())
                self.status.setText("Layer settings saved.")
            except OSError as exc:
                self.status.setText(str(exc))

    def invalidate_result(self):
        if self.result:
            self.result = ()
            for row in range(self.layers.count()):
                item = self.layers.item(row)
                item.setIcon(layer_icon(item.data(Qt.UserRole), None))
            self.output_view.set_image()
            self.status.setText("Settings changed. Solve again to refresh the layer previews.")
        self.update_actions()

    def refresh_output(self):
        row = self.layers.currentRow()
        self.output_view.set_image(self.result[-1 - row] if self.result and row >= 0 else None)

    def update_actions(self):
        count, row = self.layers.count(), self.layers.currentRow()
        if count:
            height = self.layers.sizeHintForRow(0) * min(count, 4) + 2 * self.layers.frameWidth()
            self.layers.setFixedHeight(max(62, height))
        self.remove_button.setEnabled(count > 1 and row >= 0)
        self.up_button.setEnabled(row > 0)
        self.down_button.setEnabled(0 <= row < count - 1)
        idle = self.job is None
        can_capture = idle and self.active_document() is not None
        self.choose_source_button.setEnabled(can_capture)
        self.capture_button.setEnabled(can_capture)
        self.solve_button.setEnabled(can_capture)
        applicable = bool(self.result) and all(spec.composition == "source-over" for spec in self.specs())
        self.apply_button.setEnabled(idle and applicable)
        self.apply_button.setToolTip(
            "Create an editable group of paint layers." if applicable
            else "Solve a Source over stack to apply it as Krita layers."
        )

    def solve(self):
        if self.job is not None:
            return
        if self.source is None or not self.source.matches(self.active_document()):
            self.capture_source()
        if self.source is None:
            return
        self.invalidate_result()
        image = self.source.image
        width = image.width() if self.original.isChecked() else self.width.value()
        self.job = SolveJob(
            image, self.source.working_space[1], self.specs(), width, self.backend.currentData(),
            self.opaque.isChecked(), self.refine.isChecked(), self.smooth.isChecked(),
        )
        self.controls.setEnabled(False)
        self.size_controls.setEnabled(False)
        self.update_actions()
        self.status.setText("Preparing source pixels…")
        self.job.progress.connect(self.status.setText)
        self.job.solved.connect(self.show_result)
        self.job.failed.connect(self.status.setText)
        self.job.finished.connect(self.finish_solve)
        self.job.start()

    def show_result(self, images):
        if self.source is None or not self.source.matches(self.active_document()):
            self.status.setText("Source document changed. Capture and solve again.")
            return
        self.result = images
        for row, image in enumerate(reversed(images)):
            item = self.layers.item(row)
            item.setIcon(layer_icon(item.data(Qt.UserRole), image))
        self.refresh_output()
        size = images[0].size()
        message = f"Solved {len(images)} layers at {size.width()} × {size.height()}."
        if any(spec.composition == "plus" for spec in self.specs()):
            message += " Plus stacks can be previewed here. Apply requires Source over."
        elif size != self.source.image.size():
            message += " Apply scales the layers to the canvas."
        self.status.setText(message)

    def finish_solve(self):
        self.job = None
        self.controls.setEnabled(True)
        self.size_controls.setEnabled(True)
        self.update_actions()

    def apply_result(self):
        if self.source is None or not self.result or self.job is not None:
            return
        try:
            insert_result(self.active_document(), self.source, self.specs(), self.result)
        except (ValueError, RuntimeError, MemoryError) as exc:
            self.status.setText(str(exc) or "Could not apply the layers.")
            return
        self.status.setText("Added an editable Unblending group to the document.")
