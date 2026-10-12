"""Server-side model configuration with atomic local dotenv persistence."""
from __future__ import annotations

import io
import math
import os
import stat
import tempfile
import threading
from dataclasses import replace
from pathlib import Path

from dotenv import dotenv_values
from dotenv.parser import parse_stream

from backend.model_client import ModelClient, ModelConfig, get_model_registry


FIELDS = {
    "apiUrl": ("api_url", "API_URL"),
    "apiKey": ("api_key", "API_KEY"),
    "modelName": ("model_name", "MODEL_NAME"),
    "modelVersion": ("model_version", "MODEL_VERSION"),
    "apiFormat": ("api_format", "API_FORMAT"),
    "scoreKind": ("score_kind", "SCORE_KIND"),
    "sendImage": ("send_image", "SEND_IMAGE"),
    "timeoutSeconds": ("timeout_seconds", "TIMEOUT_SECONDS"),
    "jsonMode": ("json_mode", "JSON_MODE"),
    "temperature": ("temperature", "TEMPERATURE"),
}


class ConfigurationError(Exception):
    def __init__(self, code, message, status_code=422):
        super().__init__(message)
        self.code, self.message, self.status_code = code, message, status_code


def _dotenv_literal(value):
    if isinstance(value, bool):
        text = "true" if value else "false"
    elif value is None:
        text = "omit"
    else:
        text = str(value)
    return "'" + text.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _atomic_dotenv_update(path: Path, updates: dict):
    """Rewrite a whole transaction, preserving unrelated entries and comments."""
    original = path.read_text(encoding="utf-8") if path.exists() else ""
    chunks, remaining = [], dict(updates)
    for binding in parse_stream(io.StringIO(original)):
        if binding.key in updates:
            if binding.key in remaining:
                chunks.append(f"{binding.key}={_dotenv_literal(remaining.pop(binding.key))}\n")
            # Drop duplicate assignments to the same edited variable.
        else:
            chunks.append(binding.original.string)
    content = "".join(chunks)
    if content and not content.endswith("\n"):
        content += "\n"
    content += "".join(f"{key}={_dotenv_literal(value)}\n" for key, value in remaining.items())
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n", dir=path.parent, prefix=".env-write-", delete=False) as handle:
            temporary = Path(handle.name)
            if path.exists():
                os.chmod(temporary, stat.S_IMODE(path.stat().st_mode))
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


class ModelConfigurationStore:
    def __init__(self, path: Path, registry=None, env=None):
        self.path = Path(path)
        # Capture explicit process variables before reading .env. They continue
        # to win at startup; saved browser settings win in this running process.
        self.environment = dict(os.environ if env is None else env)
        file_values = {key: value for key, value in dotenv_values(self.path, interpolate=False).items() if value is not None} if self.path.exists() else {}
        self.startup_values = {**file_values, **self.environment}
        self.registry = registry if registry is not None else get_model_registry(self.startup_values)
        self.lock = threading.RLock()

    def _config(self, provider):
        if provider not in self.registry:
            raise ConfigurationError("INVALID_PROVIDER", "未知的模型供应商。", 404)
        config = getattr(self.registry[provider], "config", None)
        if not isinstance(config, ModelConfig):
            raise ConfigurationError("CONFIGURATION_UNAVAILABLE", "该模型不使用 API 密钥配置，请查看模型详情。", 503)
        return config

    def public(self, provider):
        with self.lock:
            config = self._config(provider)
            values = {public: getattr(config, attribute) for public, (attribute, _) in FIELDS.items() if public != "apiKey"}
            # Invalid environment values must not produce non-JSON NaN fields.
            if not math.isfinite(values["timeoutSeconds"]):
                values["timeoutSeconds"] = 30
            if values["temperature"] is not None and not math.isfinite(values["temperature"]):
                values["temperature"] = None
            prefix = f"TOXILENS_{provider.upper()}_"
            overrides = [public for public, (_, suffix) in FIELDS.items() if prefix + suffix in self.environment]
            # Saving custom configuration establishes the modern CUSTOM_
            # namespace; legacy MODEL_ variables then cease to select it.
            return {"providerId": provider, **values, "hasApiKey": bool(config.api_key), "configured": config.configuration_error() is None, "restartOverrideFields": list(dict.fromkeys(overrides))}

    def save(self, provider, values):
        with self.lock:
            config = self._config(provider)
            changes = {}
            for public, value in values.items():
                attribute, _ = FIELDS[public]
                if public == "apiKey" and (value is None or not value.strip()):
                    continue
                changes[attribute] = value.strip() if isinstance(value, str) else value
            updated = replace(config, **changes)
            issue = updated.configuration_error()
            if issue:
                raise ConfigurationError("INVALID_MODEL_CONFIGURATION", issue)
            prefix = f"TOXILENS_{provider.upper()}_"
            persistent = {prefix + suffix: getattr(updated, attribute) for attribute, suffix in FIELDS.values()}
            try:
                _atomic_dotenv_update(self.path, persistent)
            except (OSError, UnicodeError):
                raise ConfigurationError("CONFIGURATION_SAVE_FAILED", "无法保存本地 API 配置，请检查配置文件权限。", 500) from None
            # Apply only after persistence succeeds. In-flight calls keep their
            # immutable old config; new calls immediately use this new client.
            self.registry[provider] = ModelClient(updated)
            return self.public(provider)
