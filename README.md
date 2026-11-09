# Unblending for Krita

> **Independent project.** This Krita plugin has no affiliation with Yuki Koyama or the original Unblending project. It uses a fork of the original Unblending library developed by Yuki Koyama. [Original Unblending repository](https://github.com/yuki-koyama/unblending)

A Krita docker that uses the Unblending SDK to decompose an image into editable paint layers. It uses Krita's Python 3 and PyQt5 bindings. The complete workflow was tested on Windows x64 with Krita 5.3.4.

## Build the native SDK

Build the shared library from the [Unblending SDK branch](https://github.com/TheDucker1/unblending/tree/feature/cuda-public-dispatcher). This plugin expects ABI 1 from that branch. Use CMake 3.18 or newer and obtain the source with its submodules.

```sh
git clone --recurse-submodules --branch feature/cuda-public-dispatcher https://github.com/TheDucker1/unblending.git
cd unblending
cmake -S . -B build-sdk -DUNBLENDING_BUILD_SDK=ON -DUNBLENDING_BUILD_CLI_APP=OFF
cmake --build build-sdk --config Release --target unblending_sdk
cmake --install build-sdk --config Release --component unblending_sdk --prefix sdk
```

For CUDA, add `-DUNBLENDING_ENABLE_CUDA=ON` and `-DCMAKE_CUDA_ARCHITECTURES=<architecture>` to the configure command. Replace `<architecture>` with the target GPU's compute capability without the decimal point. Use the NVIDIA CUDA Toolkit, a compatible compiler and driver, and a build architecture that matches your Krita installation. The commands as shown build a CPU SDK. Choose **CPU** or **Auto** in the docker when using that build.

| Platform | Installed library | Name in the plugin folder |
| --- | --- | --- |
| Windows | `sdk/bin/unblending.dll` | `unblending.dll` |
| Linux | `sdk/lib/libunblending.so` | `libunblending.so` |

On Linux, copy the actual library file under the name `libunblending.so`. The SDK install may use a symlink, so copy with `cp -L` to dereference it. The native library is built separately and ignored by Git. Its license notices are in the [upstream SDK](https://github.com/TheDucker1/unblending/tree/feature/cuda-public-dispatcher) and in `pykrita/unblending_cuda/licenses`.

## Install

1. In Krita, open **Settings → Manage Resources → Open Resource Folder**. Create a `pykrita` folder there if needed.
2. Copy `unblending_cuda.desktop` and the `unblending_cuda` directory from this repo's `pykrita` into Krita's `pykrita` folder. The default Windows path is `%APPDATA%\krita\pykrita`.
3. Put your built `unblending.dll` or `libunblending.so` inside the installed `unblending_cuda` directory, beside `__init__.py`.
4. Restart Krita. In **Settings → Configure Krita → Python Plugin Manager**, enable **Unblending CUDA**, then restart again.
5. Open **Settings → Dockers → Unblending**.

## Use

The docker scrolls when space is tight. Enlarge it or float it to keep the source, stack, and color controls in view together.

1. Click **Choose source…** to pick a layer or a group from the document tree, follow the active layer or group in Krita, or use the **Visible composite**. Selecting a source captures it. Use **Capture** again after editing it.
2. Select a layer in the docker. Pick its base color with the wheel or by clicking the source preview. Use **Brightness** for dark and light variants. The hexagon shows the color model's spread around the base color. Stripes indicate colors beyond the RGB range.
3. Add layers above the selected row, remove any selected layer, or drag to reorder. The list matches Krita's order with the front layer at the top. Double-click a name to rename it. **Open…** and **Save…** read and write the existing LayerInfos JSON format.
4. Set a solve width, or click **Original size** to solve every source pixel. **Solve** updates the selected-layer preview and stack thumbnails. The default backend is **CUDA required**.
5. Click **Apply as layers** to create a new group above the current stack. Each result becomes an editable paint layer with the chosen blend mode. A transparent source also gets a group transparency mask. Use the Layers docker to toggle the group and compare with the source. Results from a custom width are scaled back to the canvas on apply.

The source picker reads each layer's rendered pixels, including its masks and opacity. For a regular group it reads Krita's composite of the group. Use **Visible composite** for pass-through groups and effects that depend on the surrounding stack. For a filter layer, choose its enclosing regular group or the visible composite.

The blend menu exposes the SDK's 13 modes and writes the corresponding native Krita modes. SDK SoftLight uses Krita's Soft Light (SVG). Source-over stacks can be applied as native layers. Plus composition is available for solver previews. Krita's integer Linear Dodge clamps the blend result before mixing opacity, which can differ from the SDK for bright overlaps.

The solver uses normalized sRGB. The plugin reads 8-bit and 16-bit sRGB directly and uses Krita's color management to convert other supported profiles to 16-bit sRGB. The output group blends in the solver's sRGB space, and Krita converts the group's composite into the document's color space. ICC round trips can shift colors, and out-of-gamut or HDR values can clip. The source alpha is applied as an 8-bit group transparency mask. For a translucent source, compare the group on its own by hiding the original source layers.
