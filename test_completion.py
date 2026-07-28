import litellm
import os

print("Testing litellm...")
fallback_model = os.getenv("PECORINO_LLM_MODEL", "ollama/llama3")
try:
    response = litellm.completion(
        model=fallback_model,
        messages=[{"role": "user", "content": "hello"}],
        max_tokens=10
    )
    print("Success:", response.choices[0].message.content)
except Exception as e:
    print("Error:", repr(e))
