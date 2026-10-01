import os

base_dir = r"D:\RÉGIS\PROJETO CAPTUR ITAPETININGA"
files = {
    "requirements.txt": """fastapi>=0.103.0
uvicorn[standard]>=0.23.2
sqlalchemy>=2.0.20
asyncpg>=0.28.0
httpx>=0.25.0
pydantic>=2.3.0
pydantic-settings>=2.0.3
redis>=5.0.0
""",
    ".env.example": """PROJECT_NAME="Captur Webhook API"
API_PORT=8000
DATABASE_URL=postgresql+asyncpg://postgres:postgrespassword@localhost:5432/captur_db
REDIS_URL=redis://localhost:6379/0

EVOLUTION_API_URL=http://localhost:8080
EVOLUTION_GLOBAL_API_KEY=sua_chave_global_aqui

CHATWOOT_API_URL=http://localhost:3000
CHATWOOT_API_ACCESS_TOKEN=seu_access_token_aqui
CHATWOOT_ACCOUNT_ID=1
""",
    "config/settings.py": """from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    PROJECT_NAME: str = "Captur Webhook API"
    API_PORT: int = 8000
    DATABASE_URL: str
    REDIS_URL: str

    EVOLUTION_API_URL: str
    EVOLUTION_GLOBAL_API_KEY: str

    CHATWOOT_API_URL: str
    CHATWOOT_API_ACCESS_TOKEN: str
    CHATWOOT_ACCOUNT_ID: int = 1

    class Config:
        env_file = ".env"

settings = Settings()
""",
    "src/database/models.py": """from datetime import datetime
from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey, Boolean
from sqlalchemy.orm import declarative_base

Base = declarative_base()

class Conversa(Base):
    __tablename__ = 'conversas'
    
    id = Column(Integer, primary_key=True, index=True)
    remetente = Column(String(255), index=True)
    destinatario = Column(String(255), index=True)
    texto = Column(Text)
    timestamp = Column(DateTime, default=datetime.utcnow)
    agente_responsavel = Column(String(255), nullable=True)
    origem = Column(String(50)) # 'whatsapp', 'chatwoot', etc.

class EstadoAtendimento(Base):
    __tablename__ = 'estados'
    
    id = Column(Integer, primary_key=True, index=True)
    telefone = Column(String(50), unique=True, index=True)
    estado = Column(String(50), default='aguardando_opcao') # aguardando_opcao, atendimento_humano
    agente_id = Column(Integer, nullable=True)
    atualizado_em = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
""",
    "src/database/connection.py": """from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from config.settings import settings

engine = create_async_engine(settings.DATABASE_URL, echo=False)
async_session = sessionmaker(
    engine, class_=AsyncSession, expire_on_commit=False
)

async def get_db():
    async with async_session() as session:
        yield session
""",
    "src/database/__init__.py": "",
    "src/bot/state.py": """from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from src.database.models import EstadoAtendimento

async def get_estado(db: AsyncSession, telefone: str):
    result = await db.execute(select(EstadoAtendimento).filter(EstadoAtendimento.telefone == telefone))
    estado = result.scalars().first()
    if not estado:
        estado = EstadoAtendimento(telefone=telefone, estado='aguardando_opcao')
        db.add(estado)
        await db.commit()
        await db.refresh(estado)
    return estado

async def update_estado(db: AsyncSession, telefone: str, novo_estado: str, agente_id: int = None):
    estado = await get_estado(db, telefone)
    estado.estado = novo_estado
    if agente_id:
        estado.agente_id = agente_id
    await db.commit()
    await db.refresh(estado)
    return estado
""",
    "src/bot/router.py": """from src.bot.state import get_estado, update_estado
from src.services.evolution import EvolutionService
from src.services.chatwoot import ChatwootService
from src.database.models import Conversa
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import insert

VENDEDORAS = {
    "1": {"nome": "Ester", "chatwoot_id": 1},
    "2": {"nome": "Thamires", "chatwoot_id": 2}
}

async def processar_mensagem(db: AsyncSession, telefone: str, texto: str):
    estado_atual = await get_estado(db, telefone)
    
    # Salvar mensagem bruta
    conversa = Conversa(remetente=telefone, destinatario="bot", texto=texto, origem="whatsapp")
    db.add(conversa)
    await db.commit()

    if estado_atual.estado == 'aguardando_opcao':
        if texto.strip() in VENDEDORAS:
            vendedora = VENDEDORAS[texto.strip()]
            await update_estado(db, telefone, 'atendimento_humano', vendedora['chatwoot_id'])
            
            # Avisa cliente
            msg = f"Tudo certo! Transferindo você para {vendedora['nome']}."
            await EvolutionService.enviar_mensagem(telefone, msg)
            
            # Criar contato e conversa no chatwoot via API
            await ChatwootService.transferir_para_agente(telefone, vendedora['chatwoot_id'], texto)
        else:
            msg = "Olá! Bem-vindo à Captur.\\nPara quem deseja falar?\\n\\n1 - Ester\\n2 - Thamires"
            await EvolutionService.enviar_mensagem(telefone, msg)
            
    elif estado_atual.estado == 'atendimento_humano':
        # Já está com humano, apenas repassa mensagem para Chatwoot (ou Evolution já faz isso, dependendo da configuração)
        pass
""",
    "src/bot/__init__.py": "",
    "src/services/evolution.py": """import httpx
from config.settings import settings

class EvolutionService:
    @staticmethod
    async def enviar_mensagem(numero: str, texto: str):
        url = f"{settings.EVOLUTION_API_URL}/message/sendText/captur"
        headers = {"apikey": settings.EVOLUTION_GLOBAL_API_KEY}
        payload = {
            "number": numero,
            "options": {
                "delay": 1200,
                "presence": "composing"
            },
            "textMessage": {
                "text": texto
            }
        }
        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(url, json=payload, headers=headers)
                response.raise_for_status()
                return response.json()
            except Exception as e:
                print(f"Erro ao enviar mensagem via Evolution: {e}")
                return None
""",
    "src/services/chatwoot.py": """import httpx
from config.settings import settings

class ChatwootService:
    @staticmethod
    async def transferir_para_agente(telefone: str, agente_id: int, mensagem: str):
        # Lógica resumida para criar contato e transferir
        url = f"{settings.CHATWOOT_API_URL}/api/v1/accounts/{settings.CHATWOOT_ACCOUNT_ID}/contacts"
        headers = {"api_access_token": settings.CHATWOOT_API_ACCESS_TOKEN}
        
        async with httpx.AsyncClient() as client:
            try:
                # 1. Cria ou encontra contato
                # 2. Cria conversa
                # 3. Atribui agente
                pass
            except Exception as e:
                print(f"Erro ao integrar com Chatwoot: {e}")
""",
    "src/services/__init__.py": "",
    "src/api/deps.py": """from src.database.connection import get_db

# Dependências para injetar nas rotas
""",
    "src/api/routes.py": """from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession
from src.database.connection import get_db
from src.bot.router import processar_mensagem

router = APIRouter()

@router.post("/webhook/evolution")
async def evolution_webhook(request: Request, db: AsyncSession = Depends(get_db)):
    data = await request.json()
    # Verifica se é mensagem
    if data.get("event") == "messages.upsert":
        messages = data.get("data", {}).get("messages", [])
        for msg in messages:
            if not msg.get("fromMe"):
                telefone = msg.get("key", {}).get("remoteJid", "").split("@")[0]
                texto = msg.get("message", {}).get("conversation", "")
                if texto:
                    await processar_mensagem(db, telefone, texto)
    return {"status": "ok"}
""",
    "src/api/__init__.py": "",
    "src/main.py": """from fastapi import FastAPI
from src.api.routes import router
from src.database.connection import engine
from src.database.models import Base

app = FastAPI(title="Captur Webhook API")

@app.on_event("startup")
async def startup_event():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

app.include_router(router, prefix="/api")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("src.main:app", host="0.0.0.0", port=8000, reload=True)
""",
    "docker/docker-compose.yml": """version: '3'

services:
  postgres:
    image: postgres:15
    environment:
      POSTGRES_USER: postgres
      POSTGRES_PASSWORD: postgrespassword
      POSTGRES_DB: captur_db
    ports:
      - "5432:5432"
    volumes:
      - postgres_data:/var/lib/postgresql/data
    restart: always

  redis:
    image: redis:7
    ports:
      - "6379:6379"
    volumes:
      - redis_data:/data
    restart: always

  evolution-api:
    image: atendai/evolution-api:v2.1.1
    environment:
      - SERVER_URL=http://localhost:8080
      - DANGEROUS_FUNCTIONS=true
      - ADMIN_API_KEY=sua_chave_global_aqui
      - DATABASE_PROVIDER=postgresql
      - DATABASE_CONNECTION_URI=postgresql://postgres:postgrespassword@postgres:5432/captur_db?schema=public
      - DATABASE_CONNECTION_CLIENT_NAME=evolution_api
      - REDIS_URI=redis://redis:6379/1
      - REDIS_PREFIX_KEY=evolution
      - REDIS_SAVE_INSTANCES=true
    ports:
      - "8080:8080"
    depends_on:
      - postgres
      - redis
    volumes:
      - evolution_instances:/evolution/instances
    restart: always

  chatwoot_web:
    image: chatwoot/chatwoot:v3.3.1
    env_file: .env.chatwoot
    ports:
      - "3000:3000"
    depends_on:
      - postgres
      - redis
    entrypoint: docker/entrypoints/web.sh
    command: ["bundle", "exec", "rails", "s", "-p", "3000", "-b", "0.0.0.0"]
    volumes:
      - chatwoot_storage:/app/storage

volumes:
  postgres_data:
  redis_data:
  evolution_instances:
  chatwoot_storage:
""",
    "README.md": """# Projeto Captur Itapetininga - WhatsApp & Chatwoot

Infraestrutura de atendimento via WhatsApp, integrado com Evolution API e Chatwoot.

## Pré-requisitos
- Docker e Docker Compose instalados.
- Python 3.11+ para rodar o bot (FastAPI).

## Passo a Passo

### 1. Subindo a Infraestrutura (Docker)
Crie um arquivo `.env.chatwoot` na pasta `docker` se necessário (conforme docs do Chatwoot) ou use o env padrão.
Na raiz do projeto, rode:
```bash
cd docker
docker-compose up -d
```
Isso vai iniciar:
- **PostgreSQL 15** na porta 5432
- **Redis 7** na porta 6379
- **Evolution API** na porta 8080
- **Chatwoot** na porta 3000

### 2. Lendo o QR Code (Evolution API)
1. Acesse seu Postman ou crie uma requisição HTTP.
2. Crie uma instância na Evolution API (POST `http://localhost:8080/instance/create`) enviando um nome para a instância (ex: `captur`).
3. Pegue o base64 gerado (ou via endpoint de conexão) e leia com o WhatsApp do aparelho da Captur.

### 3. Configurando o Chatwoot e Atendentes
1. Acesse o painel web: `http://localhost:3000`.
2. Crie uma conta de Administrador.
3. Cadastre as **Caixas de Entrada (Inboxes)** (neste caso, a API enviará os contatos para cá).
4. Em **Agentes**, cadastre as 12 vendedoras com seus e-mails e senhas para que acessem o painel via navegador.
5. Anote o `ACCOUNT_ID`, e o `API_ACCESS_TOKEN` gerado, colocando-os no seu arquivo `.env` do projeto Python.

### 4. Rodando o Bot FastAPI
Copie o `.env.example` para `.env` e preencha as credenciais do banco e APIs:
```bash
cp .env.example .env
```
Instale as dependências:
```bash
pip install -r requirements.txt
```
Inicie a aplicação:
```bash
python -m src.main
```
A API rodará na porta `8000`. Configure a URL do webhook na Evolution API apontando para `http://<SEU_IP>:8000/api/webhook/evolution`.
"""
}

for rel_path, content in files.items():
    full_path = os.path.join(base_dir, rel_path)
    os.makedirs(os.path.dirname(full_path), exist_ok=True)
    with open(full_path, "w", encoding="utf-8") as f:
        f.write(content)

print("Projeto estruturado com sucesso!")
