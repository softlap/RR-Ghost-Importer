@echo off
rem Builds RRGhostImporter.exe from rr_ghost_importer.py (run this in the same folder as the .py file)
python -m pip install --upgrade pyinstaller pywebview
python -m PyInstaller --onefile --windowed --name RRGhostImporter --hidden-import webview rr_ghost_importer.py
echo.
echo Done. Your exe is in the "dist" folder: dist\RRGhostImporter.exe
pause
