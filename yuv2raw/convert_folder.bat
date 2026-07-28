@echo off
rem ---------------------------------------------------------------------
rem  YUV -> RAW 일괄 변환기 (Windows 실행용)
rem
rem  사용법 1) 변환할 폴더를 이 파일 위로 끌어다 놓기 (드래그 앤 드롭)
rem  사용법 2) 그냥 더블클릭한 뒤 폴더 경로를 입력
rem
rem  결과는 <입력폴더>\raw_out 에 새 파일로 생성되며, 원본은 건드리지 않습니다.
rem  옵션을 바꾸고 싶으면 아래 OPTIONS 줄을 수정하세요.
rem ---------------------------------------------------------------------
setlocal

set "OPTIONS=--out-format rgb24"

set "SCRIPT=%~dp0yuv2raw.py"
if not exist "%SCRIPT%" (
    echo [오류] yuv2raw.py 를 찾을 수 없습니다: %SCRIPT%
    goto :end
)

where python >nul 2>&1
if errorlevel 1 (
    echo [오류] python 을 찾을 수 없습니다.
    echo        https://www.python.org 에서 Python 3 을 설치한 뒤 다시 실행하세요.
    echo        설치할 때 "Add Python to PATH" 를 반드시 체크하세요.
    goto :end
)

set "TARGET=%~1"
if "%TARGET%"=="" (
    set /p "TARGET=변환할 폴더 경로를 입력하세요: "
)
if "%TARGET%"=="" (
    echo 입력된 경로가 없습니다.
    goto :end
)

echo.
echo 변환 대상: %TARGET%
echo 옵션     : %OPTIONS%
echo.
python "%SCRIPT%" "%TARGET%" %OPTIONS%

:end
echo.
pause
endlocal
