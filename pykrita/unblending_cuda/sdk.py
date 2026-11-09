from array import array
import ctypes as ct
import json
from dataclasses import dataclass
from math import isclose, isfinite, sqrt
from pathlib import Path
import sys
from threading import Lock


# Tuple order is the public unblending.h ABI. Each entry also gives the Krita blend ID.
MODES = {
    "Normal": "normal",
    "Multiply": "multiply",
    "Screen": "screen",
    "Overlay": "overlay",
    "Darken": "darken",
    "Lighten": "lighten",
    "ColorDodge": "dodge",
    "ColorBurn": "burn",
    "HardLight": "hard_light",
    "SoftLight": "soft_light_svg",
    "Difference": "diff",
    "Exclusion": "exclusion",
    "LinearDodge": "linear_dodge",
}
BLEND_LABELS = {
    "ColorDodge": "Color Dodge",
    "ColorBurn": "Burn",
    "HardLight": "Hard Light",
    "SoftLight": "Soft Light (SVG)",
    "LinearDodge": "Linear Dodge",
}
COMPOSITIONS = {"source-over": 1, "plus": 2}
BACKENDS = {"CUDA required": 2, "Auto": 0, "CPU": 1}
STATUS = {
    1: "The SDK rejected the image, layer settings, or options.",
    2: "The output buffer was too small.",
    3: "CUDA could not complete the pass.",
    4: "The solver ran out of memory.",
    5: "The solver failed.",
}
LIBRARY_NAMES = {"win32": "unblending.dll", "linux": "libunblending.so"}
SOLVER_LOCK = Lock()
BYTE_TO_UNIT = tuple(value / 255 for value in range(256))
WORD_TO_UNIT = tuple(value / 65535 for value in range(65536))
DoublePointer = ct.POINTER(ct.c_double)


class RgbView(ct.Structure):
    _fields_ = [
        ("data", DoublePointer), ("size", ct.c_size_t),
        ("width", ct.c_int32), ("height", ct.c_int32), ("row_stride", ct.c_size_t),
    ]


class NativeLayer(ct.Structure):
    _fields_ = [
        ("comp_x", ct.c_int32), ("comp_y", ct.c_int32), ("comp_z", ct.c_int32),
        ("blend_mode", ct.c_int32), ("mean", ct.c_double * 3), ("precision", ct.c_double * 9),
    ]


class NativeOptions(ct.Structure):
    _fields_ = [
        ("struct_size", ct.c_uint32), ("backend", ct.c_int32),
        ("opaque_background", ct.c_int32), ("smooth_background", ct.c_int32),
        ("target_concurrency", ct.c_int32),
    ]


def covariance_axes(variance):
    """Return the columns of the covariance's Cholesky factor as RGB offsets."""
    if isinstance(variance, (float, int)):
        if not isfinite(variance) or variance <= 0:
            raise ValueError("Variance must be finite and positive.")
        sigma = sqrt(variance)
        return ((sigma, 0, 0), (0, sigma, 0), (0, 0, sigma))

    if len(variance) != 9 or not all(map(isfinite, variance)):
        raise ValueError("Covariance must have nine finite values.")
    a, b, c, d, e, f, g, h, i = variance
    if not all(isclose(x, y, rel_tol=1e-10, abs_tol=1e-12) for x, y in ((b, d), (c, g), (f, h))):
        raise ValueError("Covariance must be symmetric.")
    try:
        l00 = sqrt(a)
        l10 = (0.5 * b + 0.5 * d) / l00
        l20 = (0.5 * c + 0.5 * g) / l00
        l11 = sqrt(e - l10 * l10)
        l21 = (0.5 * f + 0.5 * h - l20 * l10) / l11
        l22 = sqrt(i - l20 * l20 - l21 * l21)
        if not isfinite(l22) or l22 <= 0:
            raise ValueError
    except (ValueError, ZeroDivisionError) as exc:
        raise ValueError("Covariance must be positive definite.") from exc
    return ((l00, l10, l20), (0, l11, l21), (0, 0, l22))


def precision_matrix(variance):
    (a, b, c), (_, d, e), (_, _, f) = covariance_axes(variance)
    # Invert the lower-triangular factor, then multiply its transpose by itself.
    u, v, w = 1 / a, 1 / d, 1 / f
    x = -b * u * v
    y = -(c * u + e * x) * w
    z = -e * v * w
    p01, p02, p12 = x * v + y * z, y * w, z * w
    result = (u*u + x*x + y*y, p01, p02, p01, v*v + z*z, p12, p02, p12, w*w)
    if not all(map(isfinite, result)):
        raise ValueError("Covariance is too small or poorly conditioned to invert.")
    return result


def spread(variance):
    if isinstance(variance, tuple):
        return sqrt(sum(variance[index] / 3 for index in (0, 4, 8)))
    return sqrt(variance)


@dataclass(frozen=True)
class LayerSpec:
    name: str
    mean: tuple
    variance: object = 0.035
    mode: str = "Normal"
    composition: str = "source-over"

    def native(self):
        if len(self.mean) != 3 or not all(0 <= channel <= 1 for channel in self.mean):
            raise ValueError("Base color must be three RGB values between 0 and 1.")
        return NativeLayer(
            COMPOSITIONS[self.composition], 1, 1, tuple(MODES).index(self.mode),
            (ct.c_double * 3)(*self.mean), (ct.c_double * 9)(*precision_matrix(self.variance)),
        )


def read_preset(path):
    items = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if isinstance(items, dict):
        items = items.get("LayerInfos")
    if not isinstance(items, list) or not items:
        raise ValueError("Expected a nonempty LayerInfos array.")
    layers = []
    for index, item in enumerate(items, 1):
        try:
            model = item["color_model"]
            variance = model["color_variance"]
            spec = LayerSpec(
                str(item.get("name") or f"Layer {index}"),
                tuple(map(float, model["primary_color"])),
                tuple(map(float, variance)) if isinstance(variance, list) else float(variance),
                item["mode"], item["comp_op"],
            )
            spec.native()
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise ValueError(f"Layer {index} has invalid settings. {exc}") from exc
        layers.append(spec)
    return layers


def write_preset(path, layers):
    items = [
        {
            "name": layer.name, "mode": layer.mode, "comp_op": layer.composition,
            "color_model": {"primary_color": layer.mean, "color_variance": layer.variance},
        }
        for layer in layers
    ]
    Path(path).write_text(json.dumps(items, indent=2) + "\n", encoding="utf-8")


class UnblendingSdk:
    def __init__(self, path=None):
        if path is None:
            filename = LIBRARY_NAMES.get(sys.platform)
            if filename is None:
                raise RuntimeError("The plugin supports Windows and Linux native SDKs.")
            path = Path(__file__).with_name(filename)
        if not path.is_file():
            raise RuntimeError(f"Place {path.name} in the unblending_cuda plugin folder. See the README for build instructions.")
        self.dll = ct.CDLL(str(path.resolve(strict=True)))
        self.dll.unblending_abi_version.argtypes = []
        self.dll.unblending_abi_version.restype = ct.c_uint32
        version = self.dll.unblending_abi_version()
        if version != 1:
            raise RuntimeError(f"Expected native SDK ABI 1, found ABI {version}.")
        common = [ct.POINTER(RgbView), ct.POINTER(NativeLayer), ct.c_size_t]
        options = [ct.POINTER(NativeOptions)]
        buffer = [DoublePointer, ct.c_size_t]
        self.dll.unblending_unmix.argtypes = common + options + buffer
        self.dll.unblending_refine.argtypes = common + buffer + options + buffer
        self.dll.unblending_unmix.restype = ct.c_int32
        self.dll.unblending_refine.restype = ct.c_int32

    def solve(self, bgra, width, height, depth, layers, backend, opaque, refine, smooth, progress):
        pixels = memoryview(bgra).cast("H" if depth == "U16" else "B")
        count = width * height
        if count <= 0 or len(pixels) != count * 4 or not layers:
            raise ValueError("Invalid image size or empty layer stack.")
        rgb = (ct.c_double * (count * 3))()
        unit = WORD_TO_UNIT if depth == "U16" else BYTE_TO_UNIT
        for pixel in range(count):
            src, dst = 4 * pixel, 3 * pixel
            rgb[dst] = unit[pixels[src + 2]]
            rgb[dst + 1] = unit[pixels[src + 1]]
            rgb[dst + 2] = unit[pixels[src]]
        view = RgbView(rgb, len(rgb), width, height, 0)
        native = (NativeLayer * len(layers))(*(layer.native() for layer in layers))
        options = NativeOptions(ct.sizeof(NativeOptions), backend, opaque, smooth, 0)
        output = (ct.c_double * (count * 4 * len(layers)))()
        args = (ct.byref(view), native, len(native))
        with SOLVER_LOCK:
            progress("Unmixing…")
            status = self.dll.unblending_unmix(*args, ct.byref(options), output, len(output))
            self.check_status(status, "Unmix")
            if refine:
                progress("Refining mattes…")
                status = self.dll.unblending_refine(
                    *args, output, len(output), ct.byref(options), output, len(output)
                )
                self.check_status(status, "Refine")
        progress("Preparing layer previews…")
        return pack_layers(output, count, len(layers), depth)

    @staticmethod
    def check_status(status, step):
        if status:
            raise RuntimeError(f"{step} failed (SDK status {status}). {STATUS.get(status, '')}")


def pack_layers(output, count, layer_count, depth):
    """Convert SDK straight RGBA doubles to Krita's straight BGRA bytes."""
    values = memoryview(output).cast("B").cast("d")
    layers = []
    typecode, maximum = ("H", 65535) if depth == "U16" else ("B", 255)
    stride = count * 4
    for layer in range(layer_count):
        packed = array(typecode, [0]) * stride
        channels = memoryview(packed)
        offset = layer * stride
        for channel, target in enumerate((2, 1, 0, 3)):
            channel_values = values[offset + channel:offset + stride:4]
            channels[target::4] = array(typecode, (
                round(max(0.0, min(1.0, value)) * maximum) for value in channel_values
            ))
        layers.append(packed.tobytes())
    return layers
