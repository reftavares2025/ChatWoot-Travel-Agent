@echo off
title Sistema Captur - WhatsApp e Chatwoot
echo ====================================================
echo    INICIANDO SISTEMA CAPTUR ITAPETININGA 24/7
echo ====================================================

:: 1. Garantir mapeamento da unidade D:
if not exist "D:\RÉGIS\PROJETO CAPTUR ITAPETININGA" (
    subst D: "C:\PROJETO_CAPTUR" >nul 2>&1
)

:: 2. Iniciar containers Docker
echo [1/2] Verificando e iniciando containers Docker...
docker start captur_postgres captur_redis captur_evolution captur_chatwoot_web captur_chatwoot_worker captur_tunnel >nul 2>&1

:: 3. Iniciar Bot FastAPI com reinicializacao automatica se cair
echo [2/2] Iniciando Bot FastAPI (Porta 8000)...
cd /d "D:\RÉGIS\PROJETO CAPTUR ITAPETININGA"

:loop
echo [Captur Bot] Rodando em http://localhost:8000...
python -m src.main
echo [Captur Bot] Bot finalizado ou reiniciando em 5 segundos...
timeout /t 5 >nul
goto loop
