#!/usr/bin/env python3
"""Print PaddlePaddle CUDA runtime details."""

from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import sys
from pathlib import Path

os.environ.setdefault("SAM_PDF_PADDLE_VARIANT", "gpu")

from python_runtime_bootstrap import bootstrap_python_paths

BOOTSTRAPPED_PATHS = bootstrap_python_paths(Path(__file__).resolve())

import paddle


def loadable_cudnn_library() -> str | None:
    candidates = [
        "libcudnn.so",
        "libcudnn.so.9",
        "libcudnn.so.8",
        "libcudnn_cnn_infer.so.8",
        "libcudnn_ops_infer.so.8",
    ]

    for name in candidates:
        try:
            ctypes.CDLL(name)
            return name
        except OSError:
            continue

    return None


def run_command(command: list[str]) -> tuple[int, str]:
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=False)
    except Exception as exc:
        return 1, str(exc)

    output = result.stdout.strip() or result.stderr.strip()
    return result.returncode, output


def main() -> None:
    print(f"Python executable: {sys.executable}")
    if BOOTSTRAPPED_PATHS:
        print("Injected library roots:")
        for path in BOOTSTRAPPED_PATHS:
            print(f"- {path}")

    print(f"Paddle version: {paddle.__version__}")
    print(f"Compiled with CUDA: {paddle.device.is_compiled_with_cuda()}")
    print(f"CUDA_VISIBLE_DEVICES: {os.environ.get('CUDA_VISIBLE_DEVICES', '<unset>')}")

    cudnn_name = loadable_cudnn_library()
    print(f"cuDNN loadable: {cudnn_name or 'no'}")

    nvidia_smi = shutil.which("nvidia-smi")
    print(f"nvidia-smi: {nvidia_smi or 'not found'}")
    if nvidia_smi:
        code, output = run_command([nvidia_smi, "--query-gpu=name,driver_version,memory.total,memory.free", "--format=csv,noheader"])
        print(f"nvidia-smi query ok: {code == 0}")
        if output:
            print(output)

    if not paddle.device.is_compiled_with_cuda():
        return

    count = paddle.device.cuda.device_count()
    print(f"CUDA device count: {count}")
    for index in range(count):
        try:
            name = paddle.device.cuda.get_device_name(index)
        except Exception:
            name = "unknown"
        print(f"- GPU {index}: {name}")


if __name__ == "__main__":
    main()
