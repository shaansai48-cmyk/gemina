"""
Cognitive LLM Router for Gemina Agents:
Provides unified Cloud (Gemini 4 Argon + Google Search Grounding) -> Local 16GB VRAM GPU (Ollama) failover
for both free-form grounded text and strict Pydantic structured schemas.
"""
from typing import Optional, Type, TypeVar
import ollama
from pydantic import BaseModel
from google import genai
from google.genai import types

T = TypeVar("T", bound=BaseModel)


class CognitiveLLMRouter:
    """
    Handles primary Cloud Gemini inference (with optional Google Search tool grounding)
    and automatic failover to local GPU Ollama inference.
    """

    def __init__(self, api_key: str, gemini_model: str = "gemini-4-argon", ollama_model: str = "qwen2.5-coder:14b"):
        self.api_key = api_key
        self.gemini_model = "gemini-4-argon" if gemini_model == "gemini-2.5-pro" else gemini_model
        self.ollama_model = ollama_model
        self._client: Optional[genai.Client] = None

    @property
    def client(self) -> genai.Client:
        if self._client is None:
            self._client = genai.Client(api_key=self.api_key)
        return self._client

    def generate_grounded_text(
        self,
        prompt: str,
        system_instruction: Optional[str] = None,
        use_google_search: bool = True,
        temperature: float = 0.1,
        allow_ollama_fallback: bool = True,
    ) -> tuple[str, str, str]:
        """
        Returns (text_output, model_used, status).
        """
        try:
            tools = [{"google_search": {}}] if use_google_search else None
            cfg = types.GenerateContentConfig(
                system_instruction=system_instruction,
                tools=tools,
                temperature=temperature,
            )
            resp = self.client.models.generate_content(
                model=self.gemini_model,
                contents=prompt,
                config=cfg,
            )
            return (resp.text or "No response generated.").strip(), self.gemini_model, "OK"
        except Exception as e:
            if not allow_ollama_fallback:
                raise
            print(f"[LLM Router Fallback -> Ollama ({self.ollama_model})] {e}")
            messages = []
            if system_instruction:
                messages.append({"role": "system", "content": system_instruction})
            messages.append({"role": "user", "content": prompt})
            local_resp = ollama.chat(
                model=self.ollama_model,
                messages=messages,
                options={"temperature": temperature},
            )
            text = local_resp["message"]["content"].strip()
            return text, self.ollama_model, f"FALLBACK_OLLAMA ({type(e).__name__})"

    def generate_structured(
        self,
        prompt: str,
        system_instruction: str,
        schema: Type[T],
        temperature: float = 0.1,
    ) -> tuple[T, str, str]:
        """
        Generates and validates a strict Pydantic model `schema`, failing over to local Ollama GPU if cloud fails.
        Returns (validated_model, model_used, status).
        """
        try:
            resp = self.client.models.generate_content(
                model=self.gemini_model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=system_instruction,
                    response_mime_type="application/json",
                    response_schema=schema,
                    temperature=temperature,
                ),
            )
            parsed = schema.model_validate_json(resp.text)
            return parsed, self.gemini_model, "OK"
        except Exception as e:
            print(f"[Structured LLM Fallback -> 16GB VRAM Ollama ({self.ollama_model})] {e}")
            local_resp = ollama.chat(
                model=self.ollama_model,
                messages=[
                    {"role": "system", "content": system_instruction},
                    {"role": "user", "content": prompt},
                ],
                format=schema.model_json_schema(),
                options={"temperature": temperature},
            )
            parsed = schema.model_validate_json(local_resp["message"]["content"])
            return parsed, self.ollama_model, f"FALLBACK_OLLAMA ({type(e).__name__})"
