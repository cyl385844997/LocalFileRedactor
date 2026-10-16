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
REM  Usage:
REM    scripts\test.bat                      run all tests
REM    scripts\test.bat tests/test_x.py       run a single file
REM    scripts\test.bat -k keyword            any other args pass through
REM
REM  NOTE: keep this file ASCII-only. The Windows console decodes .bat
REM  files as the OEM code page (GBK here); non-ASCII comments become
REM  mojibake and cmd tries to execute them as commands.
REM ===========================================================================
setlocal
cd /d "%~dp0.."
"E:\Program Files\python\python.exe" -m pytest --basetemp=pytest_tmp %*
endlocal