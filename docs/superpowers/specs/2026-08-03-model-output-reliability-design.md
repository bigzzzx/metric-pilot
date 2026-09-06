# Model Output Reliability Design

## Goal

Make every LangGraph node consume a validated structured model response and make provider/API failures observable rather than surfacing as browser JSON parsing errors.

## Scope

- Define Pydantic response contracts for routing, query planning, SQL repair, and analysis.
- Make the model adapter send a clear JSON-object instruction for OpenAI-compatible providers and omit unsupported response-format options for local runtimes.
- Validate the provider HTTP response shape before accessing `choices[0].message.content`.
- Retry one time when a model response is syntactically valid but fails its node contract; include the validation failure in the retry instruction.
- Return structured API errors and display their message safely in the browser.

## Design

`backend/schemas.py` owns node output models. Models forbid unexpected fields and use literal values for graph routes. `backend/model_client.py` owns transport concerns only: provider request construction, provider response extraction, JSON decoding, and a typed error for retryable malformed output.

Each graph node calls a typed completion helper with its expected schema. The helper validates returned JSON using Pydantic. On the first schema failure, the node receives a corrective prompt that includes a short validation message and retries once. A second failure produces a structured `ModelOutputError`; it is not converted into a normal answer.

The `/api/chat` route catches known model, validation, and graph execution failures and responds with a stable JSON error body. The React client reads response text defensively: it parses JSON only when possible and otherwise shows the server text and HTTP status.

## Provider Compatibility

The OpenAI-compatible and vLLM adapters request JSON-object output and include an explicit `JSON` instruction. Ollama requests ordinary chat completion and relies on prompt plus Pydantic validation. Anthropic keeps its separate Messages adapter and also relies on prompt plus validation. The adapter does not assume that a provider's model discovery list guarantees inference availability.

## Verification

- Unit-test successful validation, malformed JSON, missing required fields, and retry behavior.
- Test a provider HTTP error and a 200 response with an empty `choices` array.
- Build the frontend and verify a non-JSON 500 response appears as a readable error instead of `Unexpected token`.
