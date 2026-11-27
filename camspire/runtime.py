"""ONNX-Runtime-Sessions mit GPU-Auswahl (TensorRT -> CUDA -> CPU)."""
from __future__ import annotations

import logging
import os

log = logging.getLogger("camspire.runtime")

_preloaded = False


def _preload() -> None:
    """Lädt CUDA/cuDNN aus den pip-Paketen (nvidia-*-cu12), damit kein CUDA-Base-Image nötig ist."""
    global _preloaded
    if _preloaded:
        return
    _preloaded = True
    try:
        import onnxruntime as ort
        if hasattr(ort, "preload_dlls"):
            ort.preload_dlls()
    except Exception as e:  # pragma: no cover
        log.debug("preload_dlls fehlgeschlagen: %s", e)


def available_providers() -> list[str]:
    _preload()
    import onnxruntime as ort
    return ort.get_available_providers()


def create_session(model_path: str, device: str = "auto", gpu_id: int = 0, cache_dir: str | None = None):
    _preload()
    import onnxruntime as ort

    avail = ort.get_available_providers()
    providers: list = []
    if device in ("auto", "tensorrt") and "TensorrtExecutionProvider" in avail:
        opts = {"device_id": gpu_id, "trt_fp16_enable": True}
        if cache_dir:
            os.makedirs(cache_dir, exist_ok=True)
            opts.update({"trt_engine_cache_enable": True, "trt_engine_cache_path": cache_dir})
        providers.append(("TensorrtExecutionProvider", opts))
    if device in ("auto", "cuda", "tensorrt") and "CUDAExecutionProvider" in avail:
        providers.append(("CUDAExecutionProvider", {
            "device_id": gpu_id,
            "arena_extend_strategy": "kSameAsRequested",   # VRAM sparsam
            "cudnn_conv_algo_search": "HEURISTIC",
        }))
    providers.append("CPUExecutionProvider")

    so = ort.SessionOptions()
    so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    so.log_severity_level = 3
    try:
        sess = ort.InferenceSession(model_path, sess_options=so, providers=providers)
    except Exception as e:
        log.warning("GPU-Session für %s fehlgeschlagen (%s) – falle auf CPU zurück", model_path, e)
        sess = ort.InferenceSession(model_path, sess_options=so, providers=["CPUExecutionProvider"])
    log.info("Modell %s läuft auf %s", os.path.basename(model_path), sess.get_providers()[0])
    return sess
