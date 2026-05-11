FROM nvidia/cuda:12.8.1-devel-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    build-essential \
    python3 \
    python3-pip \
    nodejs \
    npm \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
RUN python3 -m pip install --upgrade pip setuptools wheel \
    && python3 -m pip install -r /app/requirements.txt

COPY hash256_auto_miner.py /app/hash256_auto_miner.py
COPY hash256_cuda_miner.cu /app/hash256_cuda_miner.cu
COPY hash256_miner_assets /app/hash256_miner_assets

RUN nvcc -O3 -std=c++17 \
    -gencode=arch=compute_75,code=sm_75 \
    -gencode=arch=compute_86,code=sm_86 \
    -gencode=arch=compute_89,code=sm_89 \
    -gencode=arch=compute_120,code=sm_120 \
    -gencode=arch=compute_120,code=compute_120 \
    /app/hash256_cuda_miner.cu -o /app/hash256_cuda_miner.exe

ENTRYPOINT ["python3", "/app/hash256_auto_miner.py"]
CMD ["--backend", "cuda", "--cuda-devices", "all", "--loop", "--broadcast-all-rpcs", "--min-priority-gwei", "8", "--replace-pending", "2", "--poll-interval", "1"]

