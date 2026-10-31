@echo off
rem Builds RRGhostImporter.exe from rr_ghost_importer.py (run this in the same folder as the .py file)
python -m pip install --upgrade pyinstaller pywebview
set ICON=
if exist assets\icon.ico set ICON=--icon assets\icon.ico
python -m PyInstaller --onefile --windowed --name RRGhostImporter %ICON% --hidden-import webview rr_ghost_importer.py
echo.
echo Done. Your exe is in the "dist" folder: dist\RRGhostImporter.exe
pause
