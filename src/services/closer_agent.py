"""#agente — IA de fechamento assume a conversa quando a vendedora acha que vai perder a venda.

Gatilho: a atendente manda uma NOTA PRIVADA "#agente" na conversa do Chatwoot
(src/api/routes.py, chatwoot_webhook). A partir daí a IA responde direto pro
cliente pelo WhatsApp em ÁUDIO (ElevenLabs), tentando entender a objeção/dor e
conduzir ao fechamento.

A conversa pode sair do controle da IA de três formas:
1. Cliente pede um ORÇAMENTO NOVO/DIFERENTE (destino, data, pacote que a IA não
   tem como cotar) -> devolve pra vendedora preparar e enviar; assim que ela
   responder de verdade na conversa (src/api/routes.py já detecta isso), a IA
   retoma sozinha.
2. Cliente FECHA a venda -> devolve pra vendedora cuidar do pagamento, e a
   venda já entra automaticamente no financeiro (ContaReceber).
3. Risco legal (cancelamento/reembolso/Procon) -> devolve pra vendedora, sem
   tentar contornar.
4. Cliente simplesmente PARA DE RESPONDER depois da IA tentar fechar -> um
   monitor em segundo plano (monitor_timeouts_loop, chamado no startup do
   main.py) resolve a conversa sozinha depois de TIMEOUT_SEM_RESPOSTA_HORAS,
   sempre com uma nota explicando o motivo. Só resolve se a ÚLTIMA mensagem
   foi da IA/equipe (nunca resolve uma pergunta do cliente sem resposta). Se o
   cliente responder depois, o Chatwoot reabre a mesma conversa sozinho.

Sem API de preço em tempo real pra pacotes (diferente do flight_agent.py, que
trava contra a SerpAPI), o guardrail aqui é: a IA só pode citar um valor em
R$ que já apareceu de verdade nessa conversa. Qualquer "R$ X" novo que o
modelo tentar inventar é descartado e trocado por uma frase que não
compromete a empresa.
"""
import asyncio
import re
import time
from typing import Literal, Optional

import httpx
from decimal import Decimal, InvalidOperation
from pydantic import BaseModel

from config.settings import settings
from src.services.evolution import EvolutionService
from src.services.chatwoot import ChatwootService
from src.services.transcription import TranscriptionService
from src.services import financeiro
from src.bot.state import update_estado_lead, get_estado, listar_por_estado

GROQ_MODEL = "openai/gpt-oss-120b"

REGEX_ESCALAR = re.compile(r"cancel|reembols|estorn|chargeback|processo|procon", re.I)
REGEX_VALOR = re.compile(r"R\$\s*([\d\.]+,\d{2})")

TIMEOUT_SEM_RESPOSTA_HORAS = 24
INTERVALO_MONITOR_SEGUNDOS = 1800  # 30 min

NOME_POR_AGENTE_ID = {11: "Ester", 12: "Thamires", 10: "Flávia", 2: "Paula"}

SYSTEM_PROMPT = """Você é a especialista sênior de fechamento de vendas da Captur Agência de \
Viagens. A vendedora pediu que você assuma esta conversa porque acha que vai perder a venda — \
seu trabalho é entender a objeção real do cliente (preço, insegurança, indecisão, comparação \
com concorrente) e conduzir ao fechamento com empatia e firmeza comercial.

REGRAS:
1. NUNCA cite um valor em R$ que não tenha aparecido antes no histórico desta conversa. Se \
precisar falar de preço e não houver um valor já dito, diga que vai confirmar com a equipe antes \
de prosseguir.
2. NUNCA ofereça desconto, cortesia ou condição que não tenha sido mencionada antes na conversa.
3. Classifique cada mensagem do cliente no campo 'tipo_evento':
   - "pedido_orcamento": o cliente pede uma cotação NOVA ou DIFERENTE da que já está na conversa \
(outro destino, outra data, outro pacote, quer saber preço de algo ainda não cotado). Você não \
tem acesso ao sistema de cotação — isso só a vendedora faz.
   - "fechou_venda": o cliente confirmou claramente que vai comprar/fechar nas condições já \
conversadas. Preencha 'valor_fechado' com o valor em R$ exato (já mencionado antes) que ele \
fechou.
   - "risco_legal": cliente menciona cancelamento, reembolso, Procon ou processo.
   - "continuar": qualquer outra situação (objeção, dúvida, ainda decidindo).
4. Sua resposta ('mensagem') SEMPRE deve soar natural como se avisasse o cliente do que vai \
acontecer (ex: em pedido_orcamento, diga que vai pedir pra especialista preparar certinho; em \
fechou_venda, comemore e diga que a especialista vai confirmar os dados finais de pagamento).
5. Tom caloroso, direto, em português. Sua resposta vai ser enviada como ÁUDIO de voz (não texto) \
— escreva como fala, sem *negrito*, sem markdown, sem emojis, frases curtas e naturais pra ouvir.

Responda em JSON estrito: {"mensagem": string, "tipo_evento": "continuar"|"pedido_orcamento"|"fechou_venda"|"risco_legal", "valor_fechado": string ou null}."""


class RespostaFechamento(BaseModel):
    mensagem: str
    tipo_evento: Literal["continuar", "pedido_orcamento", "fechou_venda", "risco_legal"] = "continuar"
    valor_fechado: Optional[str] = None


def _valores_mencionados(historico: list[dict]) -> set[str]:
    valores = set()
    for m in historico:
        for v in REGEX_VALOR.findall(m.get("content") or ""):
            valores.add(v)
    return valores


def _sanitizar(mensagem: str, permitidos: set[str]) -> str:
    for v in REGEX_VALOR.findall(mensagem):
        if v not in permitidos:
            return "Deixa eu confirmar esse valor certinho com a nossa equipe e já te retorno aqui, só um instante!"
    return mensagem


def _valor_para_decimal(valor_str: str) -> Decimal:
    try:
        return Decimal(valor_str.replace(".", "").replace(",", "."))
    except InvalidOperation:
        return Decimal("0")


async def _obter_meta_conversa(conv_id: int) -> dict:
    """Nome do contato e nome da atendente responsável, direto da API do Chatwoot."""
    headers = {"api_access_token": settings.CHATWOOT_API_ACCESS_TOKEN, "X-Forwarded-Proto": "https"}
    url = f"{settings.CHATWOOT_API_URL}/api/v1/accounts/{settings.CHATWOOT_ACCOUNT_ID}/conversations/{conv_id}"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(url, headers=headers)
            if r.status_code != 200:
                return {}
            data = r.json()
            meta = data.get("meta", {})
            return {
                "cliente_nome": (meta.get("sender") or {}).get("name"),
                "atendente_nome": (meta.get("assignee") or {}).get("name"),
            }
    except Exception as e:
        print(f"[Closer Agent] Erro ao buscar meta da conversa {conv_id}: {e}")
        return {}


async def ativar(db, telefone: str, conv_id: int) -> None:
    await update_estado_lead(db, telefone, novo_estado="ia_fechamento", chatwoot_conv_id=conv_id)
    try:
        await ChatwootService.enviar_mensagem(
            conv_id, "🤖 IA de Fechamento assumiu esta conversa. Ela só vai citar valores que já apareceram aqui.",
            is_private=True
        )
    except Exception as e:
        print(f"[Closer Agent] Erro ao postar nota de ativação: {e}")


async def desativar(db, telefone: str, conv_id: int, motivo: str = "") -> None:
    await update_estado_lead(db, telefone, novo_estado="atendimento_humano")
    try:
        texto = f"🤖 IA de Fechamento devolveu a conversa pra você{f': {motivo}' if motivo else '.'}"
        await ChatwootService.enviar_mensagem(conv_id, texto, is_private=True)
    except Exception as e:
        print(f"[Closer Agent] Erro ao postar nota de devolução: {e}")


async def retomar_apos_orcamento(db, telefone: str) -> bool:
    """Chamado por src/api/routes.py quando a vendedora manda uma mensagem de verdade pro
    cliente enquanto a conversa estava em 'aguardando_orcamento_vendedora' — devolve o controle
    pra IA de fechamento automaticamente. Retorna True se havia uma conversa nesse estado."""
    estado_obj = await get_estado(db, telefone)
    if estado_obj.estado != "aguardando_orcamento_vendedora":
        return False
    conv_id = getattr(estado_obj, "chatwoot_conversation_id", None)
    await update_estado_lead(db, telefone, novo_estado="ia_fechamento")
    if conv_id:
        try:
            await ChatwootService.enviar_mensagem(
                conv_id, "🤖 Orçamento enviado — IA de Fechamento retomou a conversa automaticamente.",
                is_private=True
            )
        except Exception as e:
            print(f"[Closer Agent] Erro ao postar nota de retomada: {e}")
    return True


async def _gerar_resposta(historico_texto: str, mensagem_cliente: str) -> RespostaFechamento:
    import json
    payload = {
        "model": GROQ_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"Histórico da conversa:\n{historico_texto}\n\nÚltima mensagem do cliente:\n{mensagem_cliente}"},
        ],
        "temperature": 0.4,
        "response_format": {"type": "json_object"},
    }
    async with httpx.AsyncClient(timeout=20.0) as client:
        r = await client.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {settings.GROQ_API_KEY}"},
            json=payload,
        )
        r.raise_for_status()
        content = r.json()["choices"][0]["message"]["content"]
        return RespostaFechamento(**json.loads(content))


async def _enviar_ao_cliente(telefone: str, conv_id: int, mensagem_segura: str) -> None:
    audio_enviado = False
    if settings.ELEVENLABS_API_KEY:
        try:
            b64_audio = await TranscriptionService.gerar_audio_elevenlabs(mensagem_segura)
            if b64_audio:
                await EvolutionService.enviar_audio(telefone, b64_audio)
                audio_enviado = True
        except Exception as e:
            print(f"[Closer Agent] Erro ao gerar/enviar áudio ElevenLabs: {e}")

    if not audio_enviado:
        await EvolutionService.enviar_mensagem(telefone, mensagem_segura)

    try:
        prefixo = "🔊" if audio_enviado else ""
        await ChatwootService.adicionar_mensagem(conv_id, f"{prefixo} {mensagem_segura}".strip(), message_type="outgoing")
    except Exception as e:
        print(f"[Closer Agent] Erro ao espelhar mensagem no Chatwoot: {e}")


async def processar_mensagem_fechamento(db, telefone: str, texto: str, conv_id: int) -> None:
    if REGEX_ESCALAR.search(texto):
        await EvolutionService.enviar_mensagem(
            telefone, "Entendi, já vou chamar nossa equipe pra te dar atenção total nisso agora."
        )
        await desativar(db, telefone, conv_id, motivo="cliente mencionou cancelamento/reembolso/Procon")
        return

    try:
        mensagens = await ChatwootService.obter_mensagens_conversa(conv_id)
    except Exception as e:
        print(f"[Closer Agent] Erro ao buscar histórico da conversa {conv_id}: {e}")
        mensagens = []

    historico_texto = "\n".join(
        f"{'Cliente' if m.get('message_type') == 0 else 'Atendente'}: {m.get('content', '')}"
        for m in mensagens[-20:] if m.get("content")
    )
    permitidos = _valores_mencionados(mensagens)

    try:
        resultado = await _gerar_resposta(historico_texto, texto)
    except Exception as e:
        print(f"[Closer Agent] Erro ao gerar resposta: {e}")
        return

    mensagem_segura = _sanitizar(resultado.mensagem, permitidos)
    await _enviar_ao_cliente(telefone, conv_id, mensagem_segura)

    if resultado.tipo_evento == "risco_legal":
        await desativar(db, telefone, conv_id, motivo="cliente mencionou cancelamento/reembolso/Procon")
        return

    if resultado.tipo_evento == "pedido_orcamento":
        await update_estado_lead(db, telefone, novo_estado="aguardando_orcamento_vendedora", chatwoot_conv_id=conv_id)
        try:
            await ChatwootService.enviar_mensagem(
                conv_id,
                "🔔 Cliente pediu um orçamento novo/diferente — só você tem acesso ao sistema de cotação. "
                "Prepare e manda direto aqui nessa conversa; assim que você responder, a IA de Fechamento "
                "retoma sozinha de onde parou.",
                is_private=True
            )
        except Exception as e:
            print(f"[Closer Agent] Erro ao postar nota de orçamento pendente: {e}")
        return

    if resultado.tipo_evento == "fechou_venda":
        meta = await _obter_meta_conversa(conv_id)
        cliente_nome = meta.get("cliente_nome") or f"Cliente {telefone}"
        atendente_nome = meta.get("atendente_nome")

        valor_str = resultado.valor_fechado if resultado.valor_fechado in permitidos else (
            max(permitidos, key=_valor_para_decimal) if permitidos else None
        )
        try:
            if valor_str:
                conta = await financeiro.registrar_conta_receber(
                    db,
                    cliente_nome=cliente_nome,
                    valor_total=_valor_para_decimal(valor_str),
                    cliente_telefone=telefone,
                    descricao="Fechado via IA de Fechamento (#agente)",
                    atendente_nome=atendente_nome,
                )
                nota_financeiro = f"💰 Lançado automaticamente no financeiro: Conta a Receber #{conta.id} — {cliente_nome}, R$ {valor_str}."
            else:
                nota_financeiro = "⚠️ Venda fechada, mas não consegui identificar o valor exato pra lançar no financeiro — confirme e lance manualmente."
        except Exception as e:
            print(f"[Closer Agent] Erro ao registrar conta a receber: {e}")
            nota_financeiro = "⚠️ Venda fechada, mas houve um erro ao lançar no financeiro — confirme e lance manualmente."

        await desativar(db, telefone, conv_id, motivo="cliente fechou a venda — conduza o pagamento")
        try:
            await ChatwootService.enviar_mensagem(conv_id, nota_financeiro, is_private=True)
        except Exception as e:
            print(f"[Closer Agent] Erro ao postar nota financeira: {e}")


async def verificar_timeouts_fechamento(db) -> None:
    """Resolve automaticamente as conversas em que a IA de Fechamento tentou fechar a venda
    e o cliente parou de responder. Só resolve se a ÚLTIMA mensagem da conversa foi da
    IA/equipe (nunca resolve uma pergunta do cliente sem resposta)."""
    pendentes = await listar_por_estado(db, "ia_fechamento")
    agora = time.time()

    for estado_obj in pendentes:
        conv_id = getattr(estado_obj, "chatwoot_conversation_id", None)
        telefone = estado_obj.telefone
        if not conv_id:
            continue
        try:
            mensagens = await ChatwootService.obter_mensagens_conversa(conv_id)
        except Exception as e:
            print(f"[Closer Agent] Erro ao checar timeout da conversa {conv_id}: {e}")
            continue
        if not mensagens:
            continue

        ultima = mensagens[-1]
        if ultima.get("message_type") != 1:  # 1 = outgoing; só conta se a última foi da IA/equipe
            continue
        criado_em = ultima.get("created_at")
        if not criado_em or (agora - criado_em) < TIMEOUT_SEM_RESPOSTA_HORAS * 3600:
            continue

        try:
            await ChatwootService.enviar_mensagem(
                conv_id,
                f"🤖 IA de Fechamento: cliente não respondeu mais depois da tentativa de fechamento "
                f"(mais de {TIMEOUT_SEM_RESPOSTA_HORAS}h em silêncio desde a última mensagem enviada). "
                "Marcando como resolvida automaticamente — se ele responder, a conversa reabre sozinha.",
                is_private=True
            )
            await ChatwootService.resolver_conversa(conv_id)
            await update_estado_lead(db, telefone, novo_estado="atendimento_humano")
        except Exception as e:
            print(f"[Closer Agent] Erro ao resolver conversa {conv_id} por timeout: {e}")


async def monitor_timeouts_loop() -> None:
    """Roda em segundo plano pra sempre — chamado uma vez no startup do main.py."""
    from src.database.connection import AsyncSessionLocal
    while True:
        try:
            async with AsyncSessionLocal() as db:
                await verificar_timeouts_fechamento(db)
        except Exception as e:
            print(f"[Closer Agent] Erro no loop de monitoramento de timeout: {e}")
        await asyncio.sleep(INTERVALO_MONITOR_SEGUNDOS)
