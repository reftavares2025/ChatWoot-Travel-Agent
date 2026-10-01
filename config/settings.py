from pydantic_settings import BaseSettings

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
    CHATWOOT_INBOX_ID: int = 1
    CHATWOOT_INBOX_ESTER: int = 2
    CHATWOOT_INBOX_THAMIRES: int = 3
    CHATWOOT_INBOX_REA: int = 4
    CHATWOOT_INBOX_PASSAGEM: int = 1
    CHATWOOT_PANEL_URL: str = ""
    PUBLIC_BASE_URL: str = "https://appendix-smith-jackets-interval.trycloudflare.com"

    GROQ_API_KEY: str = ""
    ELEVENLABS_API_KEY: str = ""
    ELEVENLABS_VOICE_ID: str = "WPgqgCVa3mD38JuyODU6"

    # Venda self-service de passagem aérea (src/services/flight_agent.py)
    VENDA_PASSAGEM_ATIVA: bool = False
    SERPAPI_KEY: str = ""
    LLM_MODEL: str = "gpt-4o-mini"
    LITELLM_BASE_URL: str = ""
    LITELLM_API_KEY: str = ""
    LIMITE_ESCALONAMENTO_CENTAVOS: int = 3_000_000
    WEBHOOK_TOKEN_PASSAGEM: str = ""

    class Config:
        env_file = ".env"
        extra = "ignore"

settings = Settings()
