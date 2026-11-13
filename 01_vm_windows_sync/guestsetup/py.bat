@echo off
echo === INSTALL PYTHON ===
set WG=%LOCALAPPDATA%\Microsoft\WindowsApps\winget.exe
"%WG%" install Python.Python.3.12 --silent --accept-package-agreements --accept-source-agreements > C:\Users\notes\pys.log 2>&1
echo PY_EXIT=%ERRORLEVEL% >> C:\Users\notes\pys.log
refreshenv 2>nul
set PATH=C:\Users\notes\AppData\Local\Programs\Python\Python312;C:\Users\notes\AppData\Local\Programs\Python\Python312\Scripts;%PATH%
python --version >> C:\Users\notes\pys.log 2>&1
echo === INSTALL PYWINAUTO ===
python -m pip install --quiet pywinauto >> C:\Users\notes\pys.log 2>&1
echo PIP_EXIT=%ERRORLEVEL% >> C:\Users\notes\pys.log
echo ALL_DONE >> C:\Users\notes\pys.log
type C:\Users\notes\pys.log | findstr /i "exit done version"
