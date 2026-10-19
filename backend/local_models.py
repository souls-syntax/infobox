import os
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import httpx
import importlib.util

from .config import settings

os.environ.setdefault("HF_HUB_OFFLINE", settings.hf_hub_offline)
os.environ.setdefault("TRANSFORMERS_OFFLINE", settings.hf_hub_offline)

LABELS = ["work", "personal", "finance", "shopping", "travel", "newsletter", "promotion", "important", "other"]


class LocalClassifier:
    def __init__(self):
        self._lock = threading.Lock()
        self._pipeline = None
        self._tokenizer = None

    def status(self):
        configured_path = settings.classifier_model_path.strip()
        path = Path(configured_path).expanduser() if configured_path else None
        if path is None and settings.classifier_model_id:
            try:
                from huggingface_hub import try_to_load_from_cache
                cached_config = try_to_load_from_cache(settings.classifier_model_id, "config.json")
                if isinstance(cached_config, str):
                    path = Path(cached_config).parent
            except Exception:
                path = None
        required = ("config.json", "tokenizer_config.json", "tokenizer.json")
        has_weights = path is not None and any((path / name).is_file() and (path / name).stat().st_size > 0 for name in ("model.safetensors", "pytorch_model.bin"))
        files_ok = path is not None and path.is_dir() and all((path / name).is_file() and (path / name).stat().st_size > 0 for name in required) and has_weights
        runtime_ok = importlib.util.find_spec("transformers") is not None and importlib.util.find_spec("torch") is not None
        valid = files_ok and runtime_ok
        if not files_ok:
            wanted = str(Path(configured_path).expanduser()) if configured_path else f"local Hugging Face cache entry for {settings.classifier_model_id}"
            reason = f"Classifier files are incomplete or missing at {wanted}. Set CLASSIFIER_MODEL_PATH to a complete local snapshot, or leave it blank to resolve CLASSIFIER_MODEL_ID from the offline cache. No model download is attempted."
        elif not runtime_ok:
            reason = "Install the local classifier dependencies from requirements.txt."
        else:
            # Loading the small tokenizer validates usability without loading the
            # classifier weights. The cached tokenizer.json works with current
            # Transformers even when the optional sentencepiece wheel is absent.
            try:
                with self._lock:
                    if self._tokenizer is None:
                        from transformers import AutoTokenizer
                        self._tokenizer = AutoTokenizer.from_pretrained(str(path), use_fast=True, local_files_only=True)
                reason = None
            except Exception as exc:
                valid = False
                reason = f"Could not load the local DeBERTa tokenizer at {path}: {exc}. Verify the snapshot files and install requirements.txt dependencies."
        return {"available": valid, "model": str(path) if files_ok else (configured_path or settings.classifier_model_id), "resolved_path": str(path) if files_ok else None, "reason": reason}

    def classify(self, subject: str, body: str):
        status = self.status()
        if not status["available"]:
            raise RuntimeError(status["reason"])
        with self._lock:
            if self._pipeline is None:
                try:
                    from transformers import pipeline
                    self._pipeline = pipeline("zero-shot-classification", model=status["resolved_path"], tokenizer=self._tokenizer, device=-1)
                except Exception as exc:
                    raise RuntimeError(f"Could not load local classifier: {exc}") from exc
            result = self._pipeline(f"Subject: {subject}\n\n{body[:4000]}", candidate_labels=LABELS, multi_label=False)
        return result["labels"][0], "local:" + Path(status["resolved_path"]).name


class LocalReplyModel:
    def __init__(self):
        self._lock = threading.Lock()
        self._process = None
        self._server_log = None

    def _startup_error(self, prefix):
        details = ""
        if self._server_log is not None:
            try:
                self._server_log.flush()
                self._server_log.seek(0, os.SEEK_END)
                size = self._server_log.tell()
                self._server_log.seek(max(0, size - 4000))
                details = self._server_log.read(4000).decode("utf-8", "replace").strip()
            except OSError:
                pass
        if details:
            return f"{prefix} llama-server reported: {details}"
        return prefix

    def status(self):
        model = Path(settings.llama_cpp_model_path).expanduser()
        binary = shutil.which(settings.llama_server_path) or (settings.llama_server_path if Path(settings.llama_server_path).is_file() else None)
        available = model.is_file() and binary is not None and (not Path(str(binary)).is_file() or os.access(binary, os.X_OK))
        reason = None if available else (f"Qwen model file not found at {model}. Set LLAMA_CPP_MODEL_PATH." if not model.is_file() else f"llama-server executable not found at {settings.llama_server_path}. Set LLAMA_SERVER_PATH.")
        running = self._process is not None and self._process.poll() is None
        if not running:
            try:
                running = httpx.get(settings.llama_server_url.rstrip("/") + "/health", timeout=0.35).is_success
            except httpx.HTTPError:
                pass
        return {"available": available, "model": str(model) if model.is_file() else settings.llama_cpp_model_path, "server": binary, "running": running, "reason": reason}

    def _start(self):
        status = self.status()
        if not status["available"]:
            raise RuntimeError(status["reason"])
        if status["running"]:
            return
        if self._process is not None and self._process.poll() is None:
            return
        args = [str(status["server"]), "-m", str(Path(settings.llama_cpp_model_path).expanduser()), "--host", "127.0.0.1", "--port", str(httpx.URL(settings.llama_server_url).port or 40002), "-c", str(settings.llama_context_size), "-ngl", str(settings.llama_gpu_layers), "--reasoning", settings.llama_reasoning]
        try:
            # Startup diagnostics are kept locally so the UI can distinguish
            # an incompatible model, resource problem, or occupied port.
            if self._server_log is not None:
                self._server_log.close()
            self._server_log = tempfile.TemporaryFile()
            self._process = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=self._server_log)
        except OSError as exc:
            raise RuntimeError(f"Could not start llama-server: {exc}") from exc
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            if self._process.poll() is not None:
                code = self._process.returncode
                raise RuntimeError(self._startup_error(f"llama-server exited during startup (exit code {code}). Check the model, llama.cpp build, and configured port."))
            try:
                if httpx.get(settings.llama_server_url.rstrip("/") + "/health", timeout=1).is_success:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.5)
        self._process.terminate()
        try:
            self._process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._process.kill()
            self._process.wait()
        raise RuntimeError(self._startup_error("Timed out waiting for llama-server health check. The startup process was stopped. Check available memory/GPU resources, model path, and port."))

    def stop(self):
        """Stop the persistent local inference process during app shutdown."""
        with self._lock:
            process = self._process
            if process is None:
                return
            self._process = None
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            if self._server_log is not None:
                self._server_log.close()
                self._server_log = None

    def generate(self, subject: str, sender: str, body: str):
        with self._lock:
            self._start()
            try:
                response = httpx.post(settings.llama_server_url.rstrip("/") + "/v1/chat/completions", json={"messages": [{"role": "system", "content": "Write a concise, helpful email reply. Return only the reply body, without a subject or explanation."}, {"role": "user", "content": f"From: {sender}\nSubject: {subject}\nEmail:\n{body[:5000]}"}], "max_tokens": settings.llama_max_tokens, "temperature": 0.4}, timeout=180)
                response.raise_for_status()
                choice = response.json()["choices"][0]
                message = choice.get("message", {})
                content = (message.get("content") or "").strip()
            except Exception as exc:
                raise RuntimeError(f"Local Qwen draft generation failed: {exc}") from exc
            if not content:
                if message.get("reasoning_content") or choice.get("finish_reason") == "length":
                    raise RuntimeError("Local Qwen used its output budget for reasoning and returned no reply body. Restart llama-server with --reasoning off, then try again.")
                raise RuntimeError("Local Qwen returned no reply text. Check the model's chat template, then try again.")
            return content, "local-qwen:" + Path(settings.llama_cpp_model_path).name


classifier = LocalClassifier()
reply_model = LocalReplyModel()
