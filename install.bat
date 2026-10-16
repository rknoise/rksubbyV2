@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo rksubbyV2 - csomagok telepitese
echo.
python --version >nul 2>&1
if errorlevel 1 goto nopython
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
if errorlevel 1 goto hiba
echo.
choice /c IN /m "Van NVIDIA videokartyad (ajanlott)? I = igen, N = nem"
if errorlevel 2 goto kesz
python -m pip install -r requirements-gpu.txt
if errorlevel 1 goto hiba
:kesz
echo.
echo Kesz. Inditas: start.bat  (proba Xbox nelkul: start.bat --wav teszt_angol.wav)
pause
exit /b 0
:nopython
echo Nem talalom a Pythont. Telepitsd (python.org vagy Microsoft Store), majd futtasd ujra.
pause
exit /b 1
:hiba
echo.
echo Hiba tortent a telepites kozben, lasd a fenti uzeneteket.
pause
exit /b 1
