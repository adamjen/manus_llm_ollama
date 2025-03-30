# Ollama Model Resolution Plan

## Issue Analysis
The application fails with error:
```
litellm.APIConnectionError: OllamaException - {"error":"model 'ollama/qwen2.5:14b' not found"}
```

Root causes:
1. Double prefixing of "ollama/" in model name (from both config and code)
2. The model "qwen2.5:14b" may not be installed in Ollama

## Resolution Steps

### 1. Check Available Ollama Models
```bash
ollama list
```

### 2. Choose One of These Options:

#### Option A: Install Correct Model
If qwen2.5:14b exists in Ollama's model library:
```bash
ollama pull qwen2.5:14b
```

#### Option B: Use Available Model
Update config.toml to use an available model (e.g. llama3):
```toml
[llm]
model = "llama3"  # Changed from "qwen2.5:14b"
```

### 3. Code Fixes Needed
Fix double prefix in app/llm/inference.py line 443:
```python
# Change from:
model="ollama/" + model_name,
# To:
model=model_name,
```

### 4. Verification Steps
1. Restart the application
2. Test with simple prompt
3. Verify no model not found errors

## Implementation Sequence
```mermaid
graph TD
    A[Check available models] --> B{Model available?}
    B -->|Yes| C[Install model]
    B -->|No| D[Update config]
    C --> E[Fix code]
    D --> E
    E --> F[Test application]