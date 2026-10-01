import json
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from src.database.models import EstadoAtendimento

# In-memory lead data store (the DB model doesn't have a dados_lead column)
_lead_data_cache: dict[str, dict] = {}

async def get_estado(db: AsyncSession, telefone: str):
    result = await db.execute(select(EstadoAtendimento).filter(EstadoAtendimento.telefone == telefone))
    estado = result.scalars().first()
    if not estado:
        estado = EstadoAtendimento(telefone=telefone, estado='aguardando_opcao')
        db.add(estado)
        await db.commit()
        await db.refresh(estado)
    return estado

async def get_dados_lead(estado_obj) -> dict:
    if not estado_obj:
        return {}
    telefone = getattr(estado_obj, 'telefone', '')
    return _lead_data_cache.get(telefone, {})

async def update_estado_lead(
    db: AsyncSession,
    telefone: str,
    novo_estado: str,
    chave_dado: str = None,
    valor_dado: str = None,
    novos_dados: dict = None,
    agente_id: int = None,
    chatwoot_conv_id: int = None,
    atendido: bool = None
):
    estado = await get_estado(db, telefone)
    estado.estado = novo_estado
    
    dados_atuais = _lead_data_cache.get(telefone, {})
    if chave_dado and valor_dado is not None:
        dados_atuais[chave_dado] = valor_dado
    if novos_dados:
        dados_atuais.update(novos_dados)
    _lead_data_cache[telefone] = dados_atuais
    
    if agente_id and hasattr(estado, 'agente_id'):
        estado.agente_id = agente_id

    if chatwoot_conv_id and hasattr(estado, 'chatwoot_conversation_id'):
        estado.chatwoot_conversation_id = chatwoot_conv_id

    await db.commit()
    await db.refresh(estado)
    return estado

async def listar_por_estado(db: AsyncSession, estado: str) -> list[EstadoAtendimento]:
    result = await db.execute(select(EstadoAtendimento).filter(EstadoAtendimento.estado == estado))
    return list(result.scalars().all())

async def update_estado(db: AsyncSession, telefone: str, novo_estado: str, agente_id: int = None):
    return await update_estado_lead(db, telefone, novo_estado=novo_estado, agente_id=agente_id)

async def marcar_hesitacao(db: AsyncSession, telefone: str, motivo: str = ""):
    pass

async def limpar_hesitacao(db: AsyncSession, telefone: str):
    pass
