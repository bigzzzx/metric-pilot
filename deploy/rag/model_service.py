"""Small CPU-only embedding/reranker HTTP service for the Data Agent."""
from __future__ import annotations

import argparse
from typing import Union

import numpy as np
import onnxruntime as ort
import torch
import torch.nn.functional as functional
import uvicorn
from fastapi import FastAPI
from pydantic import BaseModel
from transformers import AutoModel, AutoTokenizer


parser = argparse.ArgumentParser()
parser.add_argument("--task", choices=("embedding", "reranker"), required=True)
parser.add_argument("--model-path", required=True)
parser.add_argument("--port", type=int, default=80)
args = parser.parse_args()

torch.set_num_threads(2)
tokenizer = AutoTokenizer.from_pretrained(args.model_path, local_files_only=True)
if args.task == "embedding":
    model = AutoModel.from_pretrained(args.model_path, local_files_only=True)
    model.eval()
else:
    options = ort.SessionOptions()
    options.intra_op_num_threads = 2
    options.inter_op_num_threads = 1
    options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    model = ort.InferenceSession(
        f"{args.model_path}/onnx/model.int8.onnx",
        sess_options=options,
        providers=["CPUExecutionProvider"],
    )

app = FastAPI(title=f"Data Agent {args.task} service")


class EmbedRequest(BaseModel):
    inputs: Union[str, list[str]]
    truncate: bool = True
    normalize: bool = True


class RerankRequest(BaseModel):
    query: str
    texts: list[str]
    truncate: bool = True
    raw_scores: bool = False


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "task": args.task}


@app.post("/embed")
def embed(payload: EmbedRequest) -> list[list[float]]:
    if args.task != "embedding":
        raise ValueError("This container is not an embedding service")
    inputs = [payload.inputs] if isinstance(payload.inputs, str) else payload.inputs
    encoded = tokenizer(inputs, padding=True, truncation=payload.truncate, max_length=512, return_tensors="pt")
    with torch.inference_mode():
        vectors = model(**encoded).last_hidden_state[:, 0]
        if payload.normalize:
            vectors = functional.normalize(vectors, p=2, dim=1)
    return vectors.cpu().float().tolist()


@app.post("/rerank")
def rerank(payload: RerankRequest) -> list[dict]:
    if args.task != "reranker":
        raise ValueError("This container is not a reranker service")
    pairs = [[payload.query, text] for text in payload.texts]
    encoded = tokenizer(pairs, padding=True, truncation=payload.truncate, max_length=512, return_tensors="np")
    input_names = {item.name for item in model.get_inputs()}
    scores = model.run(None, {name: value for name, value in encoded.items() if name in input_names})[0]
    scores = scores.reshape(-1).astype(np.float32)
    if not payload.raw_scores:
        scores = 1.0 / (1.0 + np.exp(-scores))
    return [
        {"index": index, "score": float(score), "text": payload.texts[index]}
        for index, score in sorted(enumerate(scores.tolist()), key=lambda item: item[1], reverse=True)
    ]


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=args.port, log_level="info")
