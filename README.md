# HASH256 Auto Miner

HASH256 CUDA auto miner packaged for GitHub and GHCR.

## Docker Pull

```bash
docker pull ghcr.io/wanshe1/hash256-auto-miner2:latest
```

Private GHCR packages require login:

```bash
echo YOUR_GITHUB_TOKEN | docker login ghcr.io -u wanshe1 --password-stdin
```

## Run

Create `.env`:

```bash
cp .env.example .env
nano .env
```

Run with Docker Compose:

```bash
docker compose up -d
docker compose logs -f
```

Run directly:

```bash
docker run --rm --gpus all --env-file .env ghcr.io/wanshe1/hash256-auto-miner2:latest
```

## Local Build

```bash
docker build -t hash256-auto-miner:local .
docker run --rm --gpus all --env-file .env hash256-auto-miner:local
```

## Update

```bash
git pull
docker compose pull
docker compose up -d
```

## Notes

- `HASH256_PRIVATE_KEY` is required.
- The default command uses CUDA, all GPUs, loop mode, broadcast to all RPCs, 8 gwei priority fee, pending replacement, and 1s polling.
- Never commit `.env`, private keys, wallet files, logs, or generated build artifacts.
