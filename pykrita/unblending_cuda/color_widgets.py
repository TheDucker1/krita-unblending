from colorsys import hsv_to_rgb
from functools import lru_cache
from math import atan2, cos, hypot, pi, sin

from PyQt5.QtCore import QPointF, QRectF, Qt, pyqtSignal
from PyQt5.QtGui import QColor, QImage, QPainter, QPen
from PyQt5.QtWidgets import QWidget

from .sdk import covariance_axes


def bgra_image(data, width, height, depth="U8"):
    if depth == "U16":
        # Krita stores BGRA words. Qt's 64-bit image format stores RGBA words.
        return QImage(data, width, height, width * 8, QImage.Format_RGBA64).rgbSwapped()
    return QImage(data, width, height, width * 4, QImage.Format_ARGB32).copy()


def bgra_bytes(image, depth):
    if depth == "U16":
        image = image.convertToFormat(QImage.Format_RGBA64).rgbSwapped()
    else:
        image = image.convertToFormat(QImage.Format_ARGB32)
    if image.isNull():
        raise MemoryError("Could not allocate a pixel buffer.")
    return image.constBits().asstring(image.sizeInBytes())


def draw_checker(painter, rect, tile=8):
    painter.fillRect(rect, QColor("#b4b4b4"))
    painter.save()
    painter.setClipRect(rect)
    for row in range(int(rect.height() / tile) + 1):
        for column in range(row % 2, int(rect.width() / tile) + 1, 2):
            painter.fillRect(QRectF(rect.x() + column * tile, rect.y() + row * tile, tile, tile),
                             QColor("#dedede"))
    painter.restore()


class ImagePreview(QWidget):
    picked = pyqtSignal(QColor)

    def __init__(self, placeholder, sample=False):
        super().__init__()
        self.image = QImage()
        self.thumbnail = QImage()
        self.placeholder = placeholder
        self.sample = sample
        self.setMinimumSize(125, 124)
        self.setMouseTracking(sample)

    def set_image(self, image=None):
        self.image = image if image is not None else QImage()
        self.thumbnail = QImage()
        self.update()

    def image_rect(self):
        if self.image.isNull():
            return QRectF()
        area = QRectF(self.rect()).adjusted(2, 2, -2, -2)
        size = self.image.size()
        size.scale(area.size().toSize(), Qt.KeepAspectRatio)
        return QRectF(area.center().x() - size.width() / 2, area.center().y() - size.height() / 2,
                      size.width(), size.height())

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), self.palette().base())
        if self.image.isNull():
            painter.setPen(self.palette().text().color())
            painter.drawText(self.rect().adjusted(8, 4, -8, -4),
                             Qt.AlignCenter | Qt.TextWordWrap, self.placeholder)
            return
        rect = self.image_rect()
        size = rect.size().toSize()
        if self.thumbnail.size() != size:
            self.thumbnail = self.image.scaled(size, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
        draw_checker(painter, rect)
        painter.drawImage(rect, self.thumbnail)

    def pixel(self, position):
        rect = self.image_rect()
        if not rect.isEmpty() and rect.contains(position):
            x = min(self.image.width() - 1, int((position.x() - rect.x()) * self.image.width() / rect.width()))
            y = min(self.image.height() - 1, int((position.y() - rect.y()) * self.image.height() / rect.height()))
            return self.image.pixelColor(x, y)
        return QColor()

    def mouseMoveEvent(self, event):
        if self.sample:
            color = self.pixel(event.localPos())
            self.setCursor(Qt.CrossCursor if color.isValid() and color.alpha() else Qt.ArrowCursor)
            self.setToolTip(color.name().upper() if color.isValid() and color.alpha() else "")

    def mousePressEvent(self, event):
        if self.sample and event.button() == Qt.LeftButton:
            color = self.pixel(event.localPos())
            if color.isValid() and color.alpha():
                self.picked.emit(color)


@lru_cache(maxsize=1)
def wheel_texture():
    size = 171
    radius = (size - 1) / 2
    data = bytearray(size * size * 4)
    for row in range(size):
        for column in range(size):
            x, y = (column - radius) / radius, (radius - row) / radius
            saturation = hypot(x, y)
            if saturation <= 1:
                r, g, b = hsv_to_rgb(atan2(y, x) / (2 * pi) % 1, saturation, 1)
                offset = (row * size + column) * 4
                data[offset:offset + 4] = bytes((round(b * 255), round(g * 255), round(r * 255), 255))
    return bgra_image(bytes(data), size, size)


class ColorWheel(QWidget):
    picked = pyqtSignal(float, float)

    def __init__(self):
        super().__init__()
        self.hue, self.saturation = 0.0, 0.0
        self.color = QColor("#808080")
        self.dragging = False
        self.setFixedSize(121, 121)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setCursor(Qt.CrossCursor)
        self.setAccessibleName("Hue and saturation")
        self.setToolTip("Around the wheel changes hue. Toward the center reduces saturation.")

    def set_color(self, hue, saturation, color):
        self.hue, self.saturation, self.color = hue, saturation, color
        self.update()

    def wheel_rect(self):
        return QRectF(self.rect()).adjusted(8, 8, -8, -8)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        rect = self.wheel_rect()
        painter.drawImage(rect, wheel_texture())
        painter.setPen(QPen(self.palette().mid().color(), 1))
        painter.drawEllipse(rect)
        distance = self.saturation * rect.width() / 2
        angle = self.hue * 2 * pi
        point = rect.center() + QPointF(distance * cos(angle), -distance * sin(angle))
        painter.setBrush(self.color)
        painter.setPen(QPen(QColor("#141414"), 3))
        painter.drawEllipse(point, 6, 6)
        painter.setPen(QPen(QColor("white"), 1.4))
        painter.drawEllipse(point, 5, 5)

    def choose(self, position, starting=False):
        rect = self.wheel_rect()
        delta = position - rect.center()
        distance = hypot(delta.x(), delta.y()) / (rect.width() / 2)
        if starting and distance > 1.07:
            return False
        hue = atan2(-delta.y(), delta.x()) / (2 * pi) % 1 if distance > 0.02 else self.hue
        self.picked.emit(hue, min(1.0, distance) if distance > 0.02 else 0.0)
        return True

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.dragging = self.choose(event.localPos(), starting=True)

    def mouseMoveEvent(self, event):
        if self.dragging:
            self.choose(event.localPos())

    def mouseReleaseEvent(self, event):
        if self.dragging and event.button() == Qt.LeftButton:
            self.choose(event.localPos())
            self.dragging = False

    def keyPressEvent(self, event):
        key = event.key()
        if key in (Qt.Key_Left, Qt.Key_Right):
            self.picked.emit((self.hue + (-0.02 if key == Qt.Key_Left else 0.02)) % 1, self.saturation)
        elif key in (Qt.Key_Up, Qt.Key_Down):
            change = 0.04 if key == Qt.Key_Up else -0.04
            self.picked.emit(self.hue, max(0, min(1, self.saturation + change)))
        else:
            super().keyPressEvent(event)


HEX_SIZE = 109


@lru_cache(maxsize=1)
def hex_weights():
    vertices = [(sin(index * pi / 3), -cos(index * pi / 3)) for index in range(6)]
    radius = HEX_SIZE / 2 - 1
    samples = []
    for row in range(HEX_SIZE):
        y = (row + 0.5 - HEX_SIZE / 2) / radius
        for column in range(HEX_SIZE):
            x = (column + 0.5 - HEX_SIZE / 2) / radius
            sector = int((atan2(x, -y) % (2 * pi)) / (pi / 3)) % 6
            x1, y1 = vertices[sector]
            x2, y2 = vertices[(sector + 1) % 6]
            determinant = x1 * y2 - x2 * y1
            u, v = (x * y2 - y * x2) / determinant, (y * x1 - x * y1) / determinant
            if u + v <= 1:
                samples.append(((row * HEX_SIZE + column) * 4, sector, u, v, (row + column) % 7 < 2))
    return samples


class VarianceHex(QWidget):
    def __init__(self):
        super().__init__()
        self.setFixedSize(121, 121)
        self.setToolTip("Center is the base color. Each corner is one standard deviation along "
                        "a color axis. Stripes mark colors outside the RGB range.")
        self.image = QImage()
        self.spec = None

    def set_model(self, spec):
        if self.spec != spec:
            self.spec = spec
            self.image = QImage()
            self.update()

    def render_model(self):
        axes = covariance_axes(self.spec.variance)
        axes = axes + tuple(tuple(-value for value in axis) for axis in axes)
        mean = self.spec.mean
        data = bytearray(HEX_SIZE * HEX_SIZE * 4)
        for offset, sector, u, v, striped in hex_weights():
            first, second = axes[sector], axes[(sector + 1) % 6]
            rgb = tuple(mean[i] + u * first[i] + v * second[i] for i in range(3))
            r, g, b = (max(0, min(1, value)) for value in rgb)
            if striped and any(value < 0 or value > 1 for value in rgb):
                ink = 0.32 if 0.21 * r + 0.72 * g + 0.07 * b < 0.5 else 0
                r, g, b = r * 0.68 + ink, g * 0.68 + ink, b * 0.68 + ink
            data[offset:offset + 4] = bytes((round(b * 255), round(g * 255), round(r * 255), 255))
        return bgra_image(bytes(data), HEX_SIZE, HEX_SIZE)

    def paintEvent(self, event):
        if self.spec is None:
            return
        if self.image.isNull():
            self.image = self.render_model()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        margin = (self.width() - HEX_SIZE) / 2
        painter.drawImage(QPointF(margin, margin), self.image)
        center = QRectF(self.rect()).center()
        painter.setBrush(QColor.fromRgbF(*self.spec.mean))
        painter.setPen(QPen(QColor("#141414"), 3))
        painter.drawEllipse(center, 4.5, 4.5)
        painter.setPen(QPen(QColor("white"), 1.2))
        painter.drawEllipse(center, 4, 4)
