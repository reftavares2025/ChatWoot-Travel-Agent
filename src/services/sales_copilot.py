"""Modo Copiloto de Vendas — sugestão de resposta pra Ester/Thamires no Chatwoot.

Roda em toda mensagem de cliente sincronizada nas caixas de Ester/Thamires
(src/api/routes.py, seção 8 do evolution_webhook). Nunca manda nada direto pro
cliente: só posta uma nota privada na conversa pra atendente ler, ajustar se
quiser, e enviar ela mesma.

O próprio modelo decide, por saída estruturada, se a mensagem do cliente
precisa de sugestão (pergunta real) ou não (agradecimento, "ok", etc) — em vez
de filtrar por lista de palavras, o que erraria variações que a gente não
previu.

Preço, desconto e disponibilidade nunca são citados como fato: o prompt exige
que a IA diga que vai confirmar no sistema, e cabe à atendente humana colar o
valor real antes de enviar. Isso é orientação por instrução (soft guardrail) —
suficiente aqui porque sempre existe uma pessoa revisando antes do envio,
diferente do motor autônomo de passagem (src/services/flight_agent.py), que
trava por código porque manda direto pro cliente.
"""
import json
from typing import Optional

import httpx
from pydantic import BaseModel

from config.settings import settings
from src.services.chatwoot import ChatwootService

GROQ_MODEL = "openai/gpt-oss-120b"

SYSTEM_PROMPT = """Você é o copiloto de vendas da Captur Agência de Viagens, apoiando as \
atendentes Ester e Thamires no Chatwoot. Sua saída é um RASCUNHO que a atendente revisa \
antes de mandar — você nunca fala direto com o cliente.

REGRAS:
1. NUNCA invente ou estime preço, taxa, diária ou disponibilidade. Se não houver esse dado \
no contexto, escreva que vai confirmar no sistema antes de fechar.
2. NUNCA prometa desconto, cortesia ou condição especial sem dizer que precisa de aprovação \
da gestão.
3. Se o cliente pedir cancelamento, reembolso ou mencionar Procon/processo, a sugestão deve \
orientar a atendente a escalar para a diretoria, não a resolver sozinha.
4. Tom consultivo, caloroso, em português, focado em qualificar (destino, datas, viajantes, \
orçamento) ou contornar objeção com valor percebido (suporte 24h, parcelamento, curadoria).
5. Se a mensagem do cliente for só um agradecimento, confirmação ou cumprimento sem pergunta \
real, não há nada a sugerir.

Responda em JSON estrito: {"precisa_sugestao": bool, "sugestao": string ou null}."""


class SugestaoResposta(BaseModel):
    precisa_sugestao: bool
    sugestao: Optional[str] = None


async def gerar_sugestao(mensagem_cliente: str, contexto: str = "") -> Optional[str]:
    if not settings.GROQ_API_KEY:
        return None
    user_content = mensagem_cliente if not contexto else f"Contexto recente:\n{contexto}\n\nÚltima mensagem do cliente:\n{mensagem_cliente}"
    payload = {
        "model": GROQ_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        "temperature": 0.3,
        "response_format": {"type": "json_object"},
    }
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {settings.GROQ_API_KEY}"},
                json=payload,
            )
            r.raise_for_status()
            content = r.json()["choices"][0]["message"]["content"]
            resultado = SugestaoResposta(**json.loads(content))
    except Exception as e:
        print(f"[Copiloto Vendas] Erro ao gerar sugestão: {e}")
        return None
    return resultado.sugestao if resultado.precisa_sugestao else None


async def sugerir_resposta_chatwoot(conversation_id: int, mensagem_cliente: str) -> None:
    contexto = ""
    try:
        mensagens = await ChatwootService.obter_mensagens_conversa(conversation_id)
        recentes = mensagens[-6:] if mensagens else []
        contexto = "\n".join(
            f"{'Cliente' if m.get('message_type') == 0 else 'Atendente'}: {m.get('content', '')}"
            for m in recentes if m.get("content")
        )
    except Exception as e:
        print(f"[Copiloto Vendas] Erro ao buscar histórico da conversa {conversation_id}: {e}")

    sugestao = await gerar_sugestao(mensagem_cliente, contexto)
    if not sugestao:
        return

    try:
        await ChatwootService.enviar_mensagem(
            conversation_id, f"🤖 *Sugestão de resposta (Copiloto IA):*\n\n{sugestao}", is_private=True
        )
    except Exception as e:
        print(f"[Copiloto Vendas] Erro ao postar nota privada na conversa {conversation_id}: {e}")
