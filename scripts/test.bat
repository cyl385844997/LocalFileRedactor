@echo off
REM ===========================================================================
REM  Unified pytest entrypoint for this project.
REM
REM  Why this wrapper exists:
REM    pytest 9.x does NOT accept "basetemp" as an ini option in
REM    pyproject.toml. It emits "Unknown config option: basetemp" and
REM    silently ignores it, so the temp dir falls back to %TEMP%.
REM    When %TEMP% points to a location blocked by policy (this machine:
REM    E:\Temp), pytest fails while collecting tests with
REM    "PermissionError: [WinError 5]".
REM
REM    --basetemp can only be passed on the command line, so it is fixed
REM    here instead.
REM
REM  Basetemp strategy:
REM    This sandbox has a STALE pytest_tmp/ directory under the repo root
REM    whose ACL is locked (Get-ChildItem / os.rmdir both fail with
REM    [WinError 5]). Pointing --basetemp at a fixed path there triggers
REM    PermissionError on every teardown. Fix: each run creates a fresh
REM    random directory under %LOCALAPPDATA%\Temp, which is per-user and
REM    clean.
REM
REM  GUI test environment (CRITICAL):
REM    The GUI tests in tests/test_gui.py and tests/test_gui_drop.py
REM    require PySide6, which on this machine lives under .verify_libs\
REM    (not on PyPI; sandbox cannot pip install 66MB wheel due to network
REM    speed ~6.4KB/s). Without .verify_libs in PYTHONPATH, the
REM    pytest-qt plugin runs `import PySide6` -> ImportError ->
REM    @pytest.importorskip kicks in -> ALL GUI tests are silently skipped
REM    with "X skipped" and exit code 0. This masks real test loss.
REM
REM    QT_QPA_PLATFORM=offscreen forces headless rendering so the same
REM    .bat works on CI / on a workstation without an X server.
REM
REM  Usage:
REM    scripts\test.bat                      run all tests
REM    scripts\test.bat tests/test_x.py      run a single file
REM    scripts\test.bat -k keyword           any other args pass through
REM
REM  NOTE: keep this file ASCII-only. The Windows console decodes .bat
REM  files as the OEM code page (GBK here); non-ASCII comments become
REM  mojibake and cmd tries to execute them as commands.
REM ===========================================================================
setlocal
cd /d "%~dp0.."

REM PYTHONPATH: prepend src (mandatory) + .verify_libs (GUI deps).
set "PYTHONPATH=src;.verify_libs"

REM Headless Qt for the GUI tests (Pytest-qt + PySide6).
set "QT_QPA_PLATFORM=offscreen"

REM Random per-run basetemp under per-user temp; avoids stale lock on
REM F:\pytest_tmp/ left over from sandbox history.
set "BASETEMP=%LOCALAPPDATA%\Temp\redactor_pytest_%RANDOM%"
if not exist "%BASETEMP%" mkdir "%BASETEMP%" 2>nul

"E:\Program Files\python\python.exe" -m pytest --basetemp="%BASETEMP%" %*
endlocal
