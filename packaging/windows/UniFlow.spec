from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, collect_submodules
root = Path(SPECPATH).parents[1]
a = Analysis([str(root/'packaging/windows/launcher.py')], pathex=[str(root/'src')],
    datas=collect_data_files('uniflow') + collect_data_files('tzdata') + collect_data_files('uniflow.infrastructure.database.migrations', include_py_files=True),
    hiddenimports=collect_submodules('uniflow') + collect_submodules('sqlalchemy.dialects.sqlite'),
    excludes=['PySide6.QtWebEngineCore','PySide6.QtWebEngineWidgets','PySide6.QtQml','PySide6.QtQuick'], noarchive=False)
# Python's older root CRT must not shadow Qt's newer bundled CRT.
import PySide6
qt = Path(PySide6.__file__).parent
for dll in ('VCRUNTIME140.dll', 'VCRUNTIME140_1.dll', 'MSVCP140.dll'):
    a.binaries = [b for b in a.binaries if b[0].casefold() != dll.casefold()]
    a.binaries.append((dll, str(qt/dll), 'BINARY'))
# Qt uses the Windows ICU API. A conda/poppler ICU from the host PATH
# exports versioned symbols and must never shadow the OS implementation.
a.binaries = [b for b in a.binaries if Path(b[0]).name.lower() not in {'icuuc.dll', 'icudt78.dll'}]
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='UniFlow', console=False,
          icon=str(root/'packaging/windows/UniFlow.ico'))
coll = COLLECT(exe, a.binaries, a.datas, name='UniFlow')
