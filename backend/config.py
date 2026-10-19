from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Keep the app isolated from any existing .env and its user-owned credentials.
    model_config = SettingsConfigDict(env_file=".env.local", extra="ignore")
    database_url: str = "sqlite:///./localmail.db"
    frontend_port: int = 40001
    google_client_id: str = ""
    google_client_secret: str = ""
    google_redirect_uri: str = "http://localhost:40000/api/gmail/callback"
    classifier_model_path: str = "/home/souls/.cache/huggingface/hub/models--MoritzLaurer--deberta-v3-base-zeroshot-v2.0/snapshots/8e7e5af5983a0ddb1a5b45a38b129ab69e2258e8"
    classifier_model_id: str = "MoritzLaurer/deberta-v3-base-zeroshot-v2.0"
    hf_hub_offline: str = "1"
    llama_cpp_model_path: str = "/home/souls/models/qwen3.5/Qwen3.5-4B-Uncensored-HauhauCS-Aggressive-Q4_K_M.gguf"
    llama_server_path: str = "/home/souls/src/llama.cpp/build/bin/llama-server"
    llama_server_url: str = "http://127.0.0.1:40002"
    llama_context_size: int = 4096
    llama_max_tokens: int = 512
    llama_gpu_layers: int = 25
    llama_reasoning: str = "off"


settings = Settings()
