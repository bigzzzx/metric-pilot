"""Request-local model timings for diagnostics and repeatable evaluations."""
from contextvars import ContextVar

from backend.model_reliability import model_node
from backend.observability import log_event

collected_metrics = ContextVar("collected_model_metrics", default=None)


def record_metric(**values):
    item = {"node": model_node.get(), **values}
    log_event("model.measurement", **item)
    collection = collected_metrics.get()
    if collection is not None:
        collection.append(item)
