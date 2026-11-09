from array import array
from dataclasses import dataclass

from krita import Krita, Selection
from PyQt5.QtCore import QByteArray, Qt, QUuid

from .color_widgets import bgra_bytes, bgra_image
from .sdk import MODES


VISIBLE_COMPOSITE = "composite"
ACTIVE_NODE = "active"
SRGB_16 = ("RGBA", "U16", "sRGB-elle-V2-srgbtrc.icc")
LAYER_TYPES = {
    "paintlayer": "Paint layer",
    "grouplayer": "Group layer",
    "filelayer": "File layer",
    "filterlayer": "Filter layer",
    "filllayer": "Fill layer",
    "clonelayer": "Clone layer",
    "vectorlayer": "Vector layer",
}


def source_limitation(node, kind):
    if kind == "grouplayer" and node.passThroughMode():
        return "A pass-through group blends with layers outside it. Choose Visible composite or an individual layer."
    if kind == "filterlayer":
        return "A filter layer depends on the layers beneath it. Choose its enclosing group or Visible composite."
    if kind not in LAYER_TYPES:
        return "Choose a layer or group that renders RGB pixels."
    return ""


def color_space(node):
    return node.colorModel(), node.colorDepth(), node.colorProfile()


def is_srgb(space):
    model, depth, profile = space
    normalized = profile.casefold()
    return model == "RGBA" and depth in ("U8", "U16") and (
        normalized in ("srgb", "srgb built-in")
        or normalized.startswith(("srgb-elle-v2-srgbtrc", "srgb iec61966"))
    )


def set_color_space(node, space):
    if color_space(node) != space and (
        not node.setColorSpace(*space) or color_space(node) != space
    ):
        raise RuntimeError(f"Krita could not convert to {space[2]} ({space[0]}/{space[1]}).")


def convert_pixels(data, width, height, source_space, working_space):
    if source_space == working_space:
        return bytes(data)
    if not all(source_space):
        raise ValueError("Krita did not report a usable color profile for the source.")

    # Use a native paint device as an ICC conversion buffer. Its tiles can extend
    # beyond the 1×1 canvas, so the scratch document has no full-size background.
    scratch = Krita.instance().createDocument(1, 1, "Unblending color conversion", *source_space, 72.0)
    if scratch is None:
        raise RuntimeError("Krita could not create a color conversion buffer.")
    try:
        scratch.setBatchmode(True)
        if color_space(scratch) != source_space:
            raise RuntimeError(f"Krita could not open the source profile {source_space[2]}.")
        layer = scratch.createNode("Conversion buffer", "paintlayer")
        if layer is None or not scratch.rootNode().addChildNode(layer, None):
            raise RuntimeError("Krita could not create a temporary paint layer.")
        pixel_size = len(layer.pixelData(0, 0, 1, 1))
        if not pixel_size or len(data) != width * height * pixel_size:
            raise ValueError("Krita returned an unexpected pixel format for the source.")
        if not layer.setPixelData(data, 0, 0, width, height):
            raise RuntimeError("Krita could not load the pixels for color conversion.")
        set_color_space(layer, working_space)
        return bytes(layer.pixelData(0, 0, width, height))
    finally:
        scratch.close()


@dataclass
class Source:
    document: object
    document_space: tuple
    working_space: tuple
    label: str
    image: object
    alpha: bytes

    def matches(self, document):
        return (
            document is not None and document == self.document
            and (document.width(), document.height()) == (self.image.width(), self.image.height())
            and color_space(document) == self.document_space
        )


def capture(document, choice):
    if document is None:
        raise ValueError("Open a Krita document first.")
    document.waitForDone()
    document_space = color_space(document)
    source_space = document_space
    width, height = document.width(), document.height()
    opacity = 255
    if choice == VISIBLE_COMPOSITE:
        data = document.pixelData(0, 0, width, height)
        label = "Visible composite"
    else:
        if choice == ACTIVE_NODE:
            node = document.activeNode()
            if node is None:
                raise ValueError("Select a layer or group in Krita’s Layers docker.")
        else:
            node = document.nodeByUniqueID(QUuid(choice))
            if node is None:
                raise ValueError("The chosen source was removed. Choose a new source.")
        kind = node.type()
        limitation = source_limitation(node, kind)
        if limitation:
            raise ValueError(limitation)
        source_space = color_space(node)
        data = node.projectionPixelData(0, 0, width, height)
        opacity = node.opacity()
        label = f"{node.name()} · {LAYER_TYPES[kind]}"
    working_space = source_space if is_srgb(source_space) else SRGB_16
    depth = working_space[1]
    data = convert_pixels(data, width, height, source_space, working_space)
    channel_type, maximum = ("H", 65535) if depth == "U16" else ("B", 255)
    if len(data) != width * height * 4 * (2 if depth == "U16" else 1):
        raise ValueError("Krita could not read pixels from that source.")
    if opacity != 255:
        # Krita applies the node's own opacity while compositing it into its parent.
        pixels = bytearray(data)
        alpha = memoryview(pixels).cast(channel_type)[3::4]
        alpha[:] = array(channel_type, ((value * opacity + 127) // 255 for value in alpha))
        data = bytes(pixels)
    alpha = memoryview(data).cast(channel_type)[3::4]
    if not any(alpha):
        raise ValueError("The source is fully transparent. Choose another layer, group, or the visible composite.")
    # Krita's transparency mask API stores one 8-bit selection value per pixel.
    mask = alpha.tobytes() if depth == "U8" else bytes(
        (value * 255 + maximum // 2) // maximum for value in alpha
    )
    if mask.count(255) == len(mask):
        mask = b""
    image = bgra_image(data, width, height, depth)
    if image.isNull():
        raise MemoryError("Could not allocate the source image.")
    return Source(document, document_space, working_space, label, image, mask)


def insert_result(document, source, specs, images):
    if not source.matches(document):
        raise ValueError("Return to the source document with its original canvas size and color profile.")
    if any(spec.composition != "source-over" for spec in specs):
        raise ValueError("Apply requires Source over composition on every layer.")
    if len(specs) != len(images) or not specs:
        raise ValueError("The solved layers are incomplete.")

    width, height = source.image.width(), source.image.height()
    group = document.createGroupLayer("Unblending")
    group.setPassThroughMode(False)
    # Blend in the same space as the SDK. Krita color-manages the group's
    # composite into the parent document's color space.
    set_color_space(group, source.working_space)
    nodes = []
    for spec, image in zip(specs, images):
        if image.size() != source.image.size():
            image = image.scaled(width, height, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
        if image.isNull():
            raise MemoryError("Could not allocate a layer at the document size.")
        node = document.createNode(spec.name, "paintlayer")
        set_color_space(node, source.working_space)
        mode = MODES[spec.mode]
        node.setBlendingMode(mode)
        if node.blendingMode() != mode:
            raise RuntimeError(f"Krita could not set the {spec.mode} blend mode.")
        if not node.setPixelData(QByteArray(bgra_bytes(image, source.working_space[1])), 0, 0, width, height):
            raise RuntimeError(f"Krita could not write pixels to {spec.name}.")
        nodes.append(node)

    mask = None
    if source.alpha:
        selection = Selection()
        selection.setPixelData(QByteArray(source.alpha), 0, 0, width, height)
        mask = document.createTransparencyMask("Source transparency")
        mask.setSelection(selection)
    if not document.rootNode().addChildNode(group, None):
        raise RuntimeError("Krita could not add the output group.")
    try:
        for node in nodes:
            if not group.addChildNode(node, None):
                raise RuntimeError(f"Krita could not add {node.name()} to the output group.")
        if mask is not None and not group.addChildNode(mask, None):
            raise RuntimeError("Krita could not add the source transparency mask.")
        document.refreshProjection()
        document.setActiveNode(group)
    except Exception:
        group.remove()
        document.refreshProjection()
        raise
    return group
