@echo off
rem ---------------------------------------------------------------------
rem  yuv2raw launcher for Windows
rem
rem  Usage 1) Drag a folder onto this file.
rem  Usage 2) Double-click, then type the folder path.
rem
rem  It asks two things:
rem    1. output type - keep the file size, convert to RGB, or both
rem    2. resolution  - only when the output needs it
rem
rem  Output goes to <folder>\raw_out. Source files are never modified.
rem
rem  To fix a resolution so you do not have to type it every time, put it
rem  in DEFAULT_SIZE below, e.g.  set "DEFAULT_SIZE=2560x1440"
rem  EXTRA is appended to every run if you need other options.
rem
rem  NOTE: keep this file ASCII-only with CRLF line endings.
rem        cmd.exe reads .bat files in the console codepage (949 in Korea),
rem        so non-ASCII bytes here corrupt line parsing.
rem  ---------------------------------------------------------------------
setlocal

set "DEFAULT_SIZE="
set "EXTRA="

set "SCRIPT=%~dp0yuv2raw.py"
if not exist "%SCRIPT%" (
    echo [ERROR] yuv2raw.py not found next to this file.
    echo         expected: %SCRIPT%
    goto :end
)

set "PY="
where py >nul 2>&1 && set "PY=py -3"
if not defined PY (
    where python >nul 2>&1 && set "PY=python"
)
if not defined PY (
    echo [ERROR] Python 3 was not found on this PC.
    echo         Install it from https://www.python.org and be sure to tick
    echo         "Add Python to PATH" during setup.
    goto :end
)

set "TARGET=%~1"
if not defined TARGET set /p "TARGET=Folder to convert: "
if not defined TARGET (
    echo [ERROR] No path was given.
    goto :end
)
rem strip a trailing backslash so the quoted path cannot escape its quote
if "%TARGET:~-1%"=="\" set "TARGET=%TARGET:~0,-1%"

echo.
echo Output type:
echo   1) same size      - keep every byte; the file size never changes (default)
echo   2) RGB24          - convert to RGB; the file becomes 2x larger
echo   3) RGB, same size - convert to RGB and keep the file size
echo                       (fewer colour steps: 4 bits per channel for 4:2:0)
set "CHOICE="
set /p "CHOICE=Choice [1]: "

set "OUTFMT=copy"
if "%CHOICE%"=="2" set "OUTFMT=rgb24"
if "%CHOICE%"=="3" set "OUTFMT=rgb-same"
set "OPTIONS=--out-format %OUTFMT%"

if "%OUTFMT%"=="copy" goto :run

echo.
set "SIZE=%DEFAULT_SIZE%"
if defined DEFAULT_SIZE (
    echo Input resolution as WxH. Press Enter to keep %DEFAULT_SIZE%.
) else (
    echo Input resolution as WxH, for example 2560x1440.
    echo Press Enter to detect it from the file name and size.
)
set /p "SIZE=Resolution: "
if defined SIZE set "OPTIONS=%OPTIONS% --size %SIZE%"

:run
if defined EXTRA set "OPTIONS=%OPTIONS% %EXTRA%"

echo.
echo target : %TARGET%
echo options: %OPTIONS%
echo.
%PY% "%SCRIPT%" "%TARGET%" %OPTIONS%

:end
echo.
pause
endlocal
