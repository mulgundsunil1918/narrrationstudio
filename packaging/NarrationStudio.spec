# PyInstaller spec for a self-contained Narration Studio.app
#
#   ./.venv/bin/pyinstaller packaging/NarrationStudio.spec --noconfirm
#
# Produces an app that runs with no Python installed and no first-run download.
# The bulk is PyTorch; PySide6 is trimmed to the five Qt modules actually used,
# which removes roughly 800 MB of WebEngine, Quick, 3D and charting libraries.

import site
from pathlib import Path

from PyInstaller.utils.hooks import (
    collect_data_files,
    collect_dynamic_libs,
    collect_submodules,
)

ROOT = Path(SPECPATH).parent
SITE = Path(site.getsitepackages()[0])

# --- data the speech stack loads from disk at runtime --------------------
datas = []

# misaki's grapheme-to-phoneme step loads this spaCy pipeline by name.
datas += collect_data_files("en_core_web_sm", include_py_files=True)
# espeak-ng phoneme tables, shipped inside espeakng_loader.
datas += collect_data_files("espeakng_loader")
# misaki ships lexicons and misc data files.
datas += collect_data_files("misaki")
datas += collect_data_files("kokoro")
# Tokenizer/vocabulary assets.
datas += collect_data_files("transformers", subdir="models", include_py_files=False)
datas += collect_data_files("language_tags")

# The application's own resources.
datas += [(str(ROOT / "app"), "app")]

# --- imports PyInstaller cannot see through -----------------------------
hiddenimports = []
hiddenimports += collect_submodules("kokoro")
hiddenimports += collect_submodules("misaki")
hiddenimports += ["en_core_web_sm", "espeakng_loader", "phonemizer", "num2words"]
hiddenimports += ["scipy.special", "sklearn.utils._typedefs"]
# torch's C extension is loaded by name from inside torch/__init__.py, which
# PyInstaller's static analysis cannot see. Without these the app starts and
# then dies on "NameError: name '_C' is not defined".
hiddenimports += [
    "torch._C",
    "torch._C._distributed_c10d",
    "torch._C._nn",
    "torch._C._fft",
    "torch._C._linalg",
    "torch._C._sparse",
    "torch._C._special",
    "torch._VF",
    "torch.jit",
    "torch.nn.functional",
]
hiddenimports += ["app", "app.__main__"]

# --- Qt modules we do not use; each is tens to hundreds of megabytes -----
EXCLUDED_QT = [
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineQuick",
    "PySide6.QtQuick", "PySide6.QtQuick3D", "PySide6.QtQml", "PySide6.Qt3DCore",
    "PySide6.Qt3DRender", "PySide6.Qt3DExtras", "PySide6.Qt3DAnimation",
    "PySide6.Qt3DInput", "PySide6.Qt3DLogic", "PySide6.QtCharts", "PySide6.QtDataVisualization",
    "PySide6.QtBluetooth", "PySide6.QtNfc", "PySide6.QtPositioning", "PySide6.QtLocation",
    "PySide6.QtSerialPort", "PySide6.QtSerialBus", "PySide6.QtRemoteObjects",
    "PySide6.QtSensors", "PySide6.QtScxml", "PySide6.QtSpatialAudio", "PySide6.QtTest",
    "PySide6.QtDesigner", "PySide6.QtHelp", "PySide6.QtUiTools", "PySide6.QtSql",
    "PySide6.QtPdf", "PySide6.QtPdfWidgets", "PySide6.QtOpenGL", "PySide6.QtOpenGLWidgets",
    "PySide6.QtPrintSupport", "PySide6.QtSvgWidgets", "PySide6.QtTextToSpeech",
    "PySide6.QtWebChannel", "PySide6.QtWebSockets", "PySide6.QtHttpServer",
]

excludes = EXCLUDED_QT + [
    # Developer tooling and things pulled in transitively but never used.
    "tkinter", "matplotlib", "IPython", "jupyter", "notebook", "pytest",
    "PyInstaller", "setuptools._distutils", "torch.testing",
    "torchvision", "torchaudio", "tensorboard",
]

a = Analysis(
    [str(ROOT / "app" / "__main__.py")],
    pathex=[str(ROOT)],
        # torch ships its shared libraries beside the package rather than in a
    # standard location, so collect them explicitly.
    binaries=collect_dynamic_libs("torch") + collect_dynamic_libs("soundfile"),
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    excludes=excludes,
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Narration Studio",
    debug=False,
    strip=False,
    upx=False,
    console=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="Narration Studio",
)

app = BUNDLE(
    coll,
    name="Narration Studio.app",
    icon=str(ROOT / "dist" / "icon" / "AppIcon.icns"),
    bundle_identifier="com.narrationstudio.app",
    version="0.1.0",
    info_plist={
        "CFBundleName": "Narration Studio",
        "CFBundleDisplayName": "Narration Studio",
        "CFBundleShortVersionString": "0.1.0",
        "NSHighResolutionCapable": True,
        "LSMinimumSystemVersion": "12.0",
        "NSHumanReadableCopyright": "Runs entirely on this Mac.",
        "CFBundleDocumentTypes": [
            {
                "CFBundleTypeName": "Subtitle file",
                "CFBundleTypeRole": "Editor",
                "CFBundleTypeExtensions": ["srt", "narration"],
            }
        ],
    },
)
