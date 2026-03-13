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
    pause
    endlocal & exit /b %EXIT_CODE%
)

set "IG_USERNAME="
set /p IG_USERNAME=Informe o username do Instagram para importar a sessao (sem @, ENTER para vazio): 

if "%IG_USERNAME%"=="" (
    echo [i] Importando sessao sem username associado.
    python "scripts\import_instagram_session.py"
) else (
    echo [i] Importando sessao para username: %IG_USERNAME%
    python "scripts\import_instagram_session.py" --username "%IG_USERNAME%"
)

set "EXIT_CODE=%ERRORLEVEL%"
if not "%EXIT_CODE%"=="0" (
    echo [erro] A sessao foi capturada, mas o import para o banco falhou.
    echo [erro] O arquivo local permanece salvo em .secrets\instagram_storage_state.json
    echo [erro] Veja a mensagem do Python acima para identificar a causa.
    pause
    endlocal & exit /b %EXIT_CODE%
)

echo [ok] Sessao importada no banco com sucesso.
pause
endlocal & exit /b %EXIT_CODE%
