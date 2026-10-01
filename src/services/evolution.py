import time
import httpx
from config.settings import settings

# IDs das mensagens que o próprio bot enviou via API, para diferenciar de uma
# atendente respondendo direto pelo app do WhatsApp (ambas chegam como fromMe=true no webhook).
_mensagens_enviadas_pelo_bot: dict[str, float] = {}

class EvolutionService:
    @staticmethod
    async def enviar_mensagem(numero: str, texto: str, instance: str = "captur"):
        url = f"{settings.EVOLUTION_API_URL}/message/sendText/{instance}"
        headers = {"apikey": settings.EVOLUTION_GLOBAL_API_KEY}
        payload = {
            "number": numero,
            "text": texto,
            "delay": 1200
        }
        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(url, json=payload, headers=headers)
                print(f"[Evolution API] Envio para {numero}: status={response.status_code}, resp={response.text}")
                response.raise_for_status()
                resp_json = response.json()
                msg_id = (resp_json or {}).get("key", {}).get("id")
                if msg_id:
                    if len(_mensagens_enviadas_pelo_bot) > 5000:
                        _mensagens_enviadas_pelo_bot.clear()
                    _mensagens_enviadas_pelo_bot[msg_id] = time.time()
                return resp_json
            except Exception as e:
                print(f"Erro ao enviar mensagem via Evolution: {e}")
                return None

    @staticmethod
    def foi_enviado_pelo_bot(msg_id: str) -> bool:
        """True se este ID de mensagem foi enviado pelo proprio bot (evita tratar o eco
        da mensagem do bot como se fosse uma atendente respondendo direto no app)."""
        if not msg_id:
            return False
        return msg_id in _mensagens_enviadas_pelo_bot

    @staticmethod
    async def enviar_documento(numero: str, base64_data: str, filename: str, caption: str = "", instance: str = "captur"):
        url = f"{settings.EVOLUTION_API_URL}/message/sendMedia/{instance}"
        headers = {"apikey": settings.EVOLUTION_GLOBAL_API_KEY}
        payload = {
            "number": numero,
            "mediatype": "document",
            "media": base64_data,
            "fileName": filename,
            "caption": caption
        }
        async with httpx.AsyncClient(timeout=40.0) as client:
            try:
                response = await client.post(url, json=payload, headers=headers)
                print(f"[Evolution API Documento] Envio para {numero} ({filename}): status={response.status_code}")
                return response.json()
            except Exception as e:
                print(f"Erro ao enviar documento via Evolution: {e}")
                return None

    @staticmethod
    async def enviar_audio(numero: str, base64_audio: str, instance: str = "captur"):
        url = f"{settings.EVOLUTION_API_URL}/message/sendWhatsAppAudio/{instance}"
        headers = {"apikey": settings.EVOLUTION_GLOBAL_API_KEY}
        payload = {
            "number": numero,
            "audio": base64_audio,
            "delay": 1200,
            "encoding": True
        }
        async with httpx.AsyncClient(timeout=40.0) as client:
            try:
                response = await client.post(url, json=payload, headers=headers)
                print(f"[Evolution API Áudio] Envio para {numero}: status={response.status_code}")
                response.raise_for_status()
                resp_json = response.json()
                msg_id = (resp_json or {}).get("key", {}).get("id")
                if msg_id:
                    if len(_mensagens_enviadas_pelo_bot) > 5000:
                        _mensagens_enviadas_pelo_bot.clear()
                    _mensagens_enviadas_pelo_bot[msg_id] = time.time()
                return resp_json
            except Exception as e:
                print(f"Erro ao enviar áudio via Evolution: {e}")
                return None

