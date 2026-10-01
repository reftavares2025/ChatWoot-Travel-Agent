import os
import asyncio
from pathlib import Path
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from src.api.routes import router
from src.api.routes_contracts import router as contracts_router
from src.database.connection import engine
from src.database.models import Base
from src.bot.sla_monitor import sla_monitor_loop
from src.services import flight_agent

app = FastAPI(title="Captur Webhook & Contract Signing API")

STATIC_DIR = Path(__file__).resolve().parent / "static"
STATIC_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

@app.on_event("startup")
async def startup_event():
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    except Exception as e:
        print(f"[Aviso DB] Não foi possível conectar ao banco local agora: {e}")
    
    # Inicia o monitor de SLA em segundo plano
    try:
        asyncio.create_task(sla_monitor_loop())
    except Exception as e:
        print(f"[Aviso SLA] Não foi possível iniciar SLA loop: {e}")

    # Grafo de venda de passagem aérea (fica inerte se VENDA_PASSAGEM_ATIVA=False)
    try:
        await flight_agent.iniciar_checkpointer()
    except Exception as e:
        print(f"[Aviso Venda Passagem] Não foi possível iniciar o grafo: {e}")

app.include_router(router)
app.include_router(router, prefix="/api")
app.include_router(contracts_router)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("src.main:app", host="0.0.0.0", port=8000, reload=True)
