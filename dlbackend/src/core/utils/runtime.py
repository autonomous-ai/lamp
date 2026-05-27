import logging
from pathlib import Path

import numpy as np
import onnxruntime as ort

logger = logging.getLogger(__name__)


def prepare_ort_session(
    model_path: Path,
    *,
    warmup_inputs: dict[str, np.ndarray] | None = None,
) -> ort.InferenceSession:
    """Create an ONNX Runtime session with CUDA arena sharing.

    Args:
        model_path: Path to the ONNX model file.
        warmup_inputs: If provided, run a single forward pass after creation
            to pre-allocate CUDA workspace buffers at peak size.
    """
    opts = ort.SessionOptions()
    opts.intra_op_num_threads = 0
    opts.inter_op_num_threads = 0
    opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    opts.add_session_config_entry("session.dynamic_block_base", "4")

    providers: list[str | tuple[str, dict]] = []
    if "CUDAExecutionProvider" in ort.get_available_providers():
        providers.append((
            "CUDAExecutionProvider",
            {
                "arena_extend_strategy": "kSameAsRequested",
                "cudnn_conv_algo_search": "DEFAULT",
                "do_copy_in_default_stream": True,
            },
        ))
    providers.append("CPUExecutionProvider")

    session = ort.InferenceSession(str(model_path), sess_options=opts, providers=providers)

    if warmup_inputs is not None:
        logger.info("Warming up ONNX session for %s", model_path.name)
        session.run(None, warmup_inputs)
        logger.info("Warmup complete for %s", model_path.name)

    return session
