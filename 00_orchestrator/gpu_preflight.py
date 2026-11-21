"""Fail-closed preflight proving that Paddle can execute a real CUDA op.

Kept in a module (rather than an inline ``python -c``) so each failure mode can
be unit-tested with a fake paddle. A CPU default device, a missing GPU or a
broken CUDA kernel all raise; only an actual device-0 tensor op passes.
"""
from __future__ import annotations

import sys
from types import SimpleNamespace
from typing import Any


def check(paddle: Any) -> str:
    """Return a success message only after a verified CUDA round-trip."""
    if not paddle.is_compiled_with_cuda():
        raise RuntimeError("Paddle was not built with CUDA")
    if paddle.device.cuda.device_count() < 1:
        raise RuntimeError("No CUDA device is visible to Paddle")
    tensor = paddle.to_tensor([1.0], place=paddle.CUDAPlace(0))
    if not tensor.place.is_gpu_place():
        raise RuntimeError("Tensor is not on GPU; refusing CPU fallback")
    result = float((tensor * 2).numpy().item())
    if result != 2.0:
        raise RuntimeError(f"CUDA op returned an unexpected result: {result}")
    return "GPU OK: CUDA device 0 executed a tensor op"


def main(paddle: Any | None = None) -> int:
    """CLI entry point; print diagnostics and exit non-zero on any failure."""
    try:
        if paddle is None:
            import paddle as runtime_paddle

            paddle = runtime_paddle
        print(check(paddle))
        return 0
    except Exception as exc:  # fail closed, never fall back to CPU
        print(f"GPU preflight failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
