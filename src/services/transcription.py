import base64
import httpx
from config.settings import settings

class TranscriptionService:
    @staticmethod
    async def obter_audio_evolution(msg_id: str, instance_name: str = "captur", msg_dict: dict = None) -> bytes:
        """Obtém os bytes do áudio enviado no WhatsApp através da Evolution API."""
        url = f"{settings.EVOLUTION_API_URL}/chat/getBase64FromMediaMessage/{instance_name}"
        headers = {"apikey": settings.EVOLUTION_GLOBAL_API_KEY}
        payload = {"message": msg_dict or {}, "convertToMp4": False}
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(url, json=payload, headers=headers)
                if resp.status_code in (200, 201):
                    data = resp.json()
                    b64_str = data.get("base64")
                    if b64_str:
                        if "," in b64_str:
                            b64_str = b64_str.split(",")[1]
                        return base64.b64decode(b64_str)
                print(f"[TranscriptionService] Falha ao obter base64 media: status={resp.status_code}, resp={resp.text[:200]}")
        except Exception as e:
            print(f"[TranscriptionService] Erro ao obter áudio da Evolution: {e}")
        return None

    @staticmethod
    async def transcrever_audio_bytes(audio_bytes: bytes, filename: str = "audio.ogg") -> str:
        """Transcreve áudio em bytes usando a API do Whisper na Groq."""
        if not audio_bytes:
            return ""

        groq_key = settings.GROQ_API_KEY
        if groq_key:
            url = "https://api.groq.com/openai/v1/audio/transcriptions"
            headers = {"Authorization": f"Bearer {groq_key}"}
            files = {"file": (filename, audio_bytes, "audio/ogg")}
            data = {"model": "whisper-large-v3", "language": "pt"}
            try:
                async with httpx.AsyncClient(timeout=30.0) as client:
                    resp = await client.post(url, headers=headers, files=files, data=data)
                    if resp.status_code in (200, 201):
                        res_json = resp.json()
                        texto = res_json.get("text", "").strip()
                        print(f"[TranscriptionService] Transcrição Groq Whisper: '{texto}'")
                        return texto
                    else:
                        print(f"[TranscriptionService] Erro Groq Whisper: status={resp.status_code}, body={resp.text[:200]}")
            except Exception as e:
                print(f"[TranscriptionService] Exceção Groq Whisper: {e}")

        return ""

    @staticmethod
    async def gerar_audio_elevenlabs(texto: str, voice_id: str = None) -> str:
        """Gera áudio usando a API do ElevenLabs e retorna como string base64 pronta para envio via Evolution API."""
        key = settings.ELEVENLABS_API_KEY
        if not texto or not key:
            print("[ElevenLabs] API Key não configurada ou texto vazio.")
            return None

        # Vozes preferidas em ordem: a voz custom solicitada pelo usuário, depois vozes padrão de fallback
        preferred_voices = [
            voice_id or settings.ELEVENLABS_VOICE_ID or "WPgqgCVa3mD38JuyODU6",
            "cgSgspJ2msm6clMCkdW9",  # Jessica
            "EXAVITQu4vr4xnSDxMaL",  # Bella
            "21m00Tcm4TlvDq8ikWAM",  # Rachel
        ]

        async with httpx.AsyncClient(timeout=30.0) as client:
            for v_id in preferred_voices:
                if not v_id:
                    continue
                url = f"https://api.elevenlabs.io/v1/text-to-speech/{v_id}"
                headers = {
                    "xi-api-key": key,
                    "Content-Type": "application/json",
                    "Accept": "audio/mpeg"
                }
                payload = {
                    "text": texto,
                    "model_id": "eleven_multilingual_v2",
                    "voice_settings": {
                        "stability": 0.5,
                        "similarity_boost": 0.75
                    }
                }
                try:
                    resp = await client.post(url, json=payload, headers=headers)
                    if resp.status_code in (200, 201):
                        audio_b64 = base64.b64encode(resp.content).decode("utf-8")
                        print(f"[ElevenLabs] Áudio gerado com sucesso usando a voz '{v_id}' ({len(resp.content)} bytes).")
                        return audio_b64
                    else:
                        print(f"[ElevenLabs] Erro com voz '{v_id}': status={resp.status_code}, body={resp.text[:200]}")
                except Exception as e:
                    print(f"[ElevenLabs] Exceção com voz '{v_id}': {e}")

        return None
