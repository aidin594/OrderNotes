@echo off
setlocal
cd /d "%~dp0"
echo Running automated tests...
py -3 -m unittest discover -s tests -v
if errorlevel 1 goto :fail
echo Running compact GUI check...
py -3 tests\gui_smoke.py
if errorlevel 1 goto :fail
echo Installing PyInstaller for packaging...
py -3 -m pip install --upgrade "pyinstaller>=6.10,<7"
if errorlevel 1 goto :fail
echo Building a lightweight Windows app...
REM The spec file is written to build\, so all resource paths MUST be absolute.
REM Relative --add-data paths would incorrectly resolve under build\assets.
set "ICON_PATH=%CD%\assets\order_notes.ico"
set "ICON_PNG=%CD%\assets\order_notes.png"
if not exist "%ICON_PATH%" (
    echo ERROR: Missing icon file: "%ICON_PATH%"
    goto :fail
)
if not exist "%ICON_PNG%" (
    echo ERROR: Missing taskbar icon PNG: "%ICON_PNG%"
    goto :fail
)
py -3 -m PyInstaller --noconfirm --clean --onedir --windowed --name OrderNotes --icon "%ICON_PATH%" --add-data "%ICON_PATH%;assets" --add-data "%ICON_PNG%;assets" --distpath "build\output" --workpath "build\work" --specpath "build" app.py
if errorlevel 1 goto :fail

echo Updating portable folder without deleting customer data...
if not exist "OrderNotes_Portable" mkdir "OrderNotes_Portable"
robocopy "build\output\OrderNotes" "OrderNotes_Portable" /E /R:2 /W:1 /NFL /NDL /NJH /NJS >nul
if errorlevel 8 goto :fail

REM If you ran the source app before building, preserve its portable data.
if not exist "OrderNotes_Portable\data\order_notes.db" if exist "data\order_notes.db" (
    echo Copying source-mode database to the portable EXE folder...
    xcopy "data\*" "OrderNotes_Portable\data\" /E /I /Y /Q >nul
    if errorlevel 1 goto :fail
)
echo.
echo Build complete: OrderNotes_Portable\OrderNotes.exe
 echo Keep and move the whole OrderNotes_Portable folder, including data and _internal.
echo Database: OrderNotes_Portable\data\order_notes.db
pause
exit /b 0
:fail
echo.
echo Build failed. No portable customer database was deleted.
pause
exit /b 1
