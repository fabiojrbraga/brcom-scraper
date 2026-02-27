@echo off
setlocal

rem Garante execucao a partir da raiz do projeto
cd /d "%~dp0\.."

if not exist "venv\Scripts\activate.bat" (
    echo [erro] Ambiente virtual nao encontrado em venv\Scripts\activate.bat
    exit /b 1
)

call "venv\Scripts\activate.bat"
python "scripts\capture_instagram_session.py" %*
if errorlevel 1 (
    set "EXIT_CODE=%ERRORLEVEL%"
    echo [erro] Captura falhou. Import nao sera executado.
    endlocal & exit /b %EXIT_CODE%
)

set "IG_USERNAME="
set /p IG_USERNAME=Informe o username do Instagram para importar a sessao (sem @, ENTER para vazio): 

if "%IG_USERNAME%"=="" (
    echo [i] Importando sessao sem username associado.
    python "scripts\import_instagram_session.py"
) else (
    python "scripts\import_instagram_session.py" --username "%IG_USERNAME%"
)

set "EXIT_CODE=%ERRORLEVEL%"
endlocal & exit /b %EXIT_CODE%
