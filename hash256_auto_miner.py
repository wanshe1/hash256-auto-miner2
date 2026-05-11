from __future__ import annotations

import argparse
import getpass
import json
import os
import queue
import secrets
import shutil
import subprocess
import sys
import threading
import time
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional

warnings.filterwarnings("ignore", message="Unable to find acceptable character detection dependency.*")

import requests
from eth_account import Account
from eth_utils import to_checksum_address
from web3 import Web3


CONTRACT = "0xAC7b5d06fa1e77D08aea40d46cB7C5923A87A0cc"
RPC_URL = ",".join([
    "https://ethereum-public.nodies.app/",
    "https://ethereum-rpc.publicnode.com",
    "https://eth.llamarpc.com",
    "https://1rpc.io/eth",
    "https://rpc.flashbots.net/fast",
    "https://eth.drpc.org",
    "https://ethereum.public.blockpi.network/v1/rpc/public",
    "https://eth.merkle.io",
    "https://0xrpc.io/eth",
    "https://rpc.mevblocker.io/fast",
])
SITE = "https://hash256.org"

SEL_MINE = "4d474898"              # mine(uint256)
SEL_GET_CHALLENGE = "f37381ad"     # getChallenge(address)
SEL_MINING_STATE = "f06d67bb"      # miningState()


class ChainCache:
    def __init__(self, rpc: str, address: str, interval: float, ttl: float):
        self.rpc = rpc
        self.address = address
        self.interval = max(0.2, interval)
        self.ttl = max(0.2, ttl)
        self.chain_id = 0
        self.base_fee = 0
        self.account_nonce = 0
        self.priority_fee = 0
        self.last_update = 0.0
        self.last_error = ""
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> bool:
        ok = self._refresh()
        if not ok:
            log(f"chain cache warmup failed: {self.last_error}")
            return False
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        log(f"chain cache started chainId={self.chain_id} interval={self.interval:.2f}s ttl={self.ttl:.2f}s")
        return True

    def stop(self) -> None:
        self._stop.set()

    def _refresh(self) -> bool:
        try:
            chain_id = self.chain_id or int(rpc_call(self.rpc, "eth_chainId", [], timeout=8), 16)
            latest = rpc_call(self.rpc, "eth_getBlockByNumber", ["latest", False], timeout=8)
            base_fee = int(latest.get("baseFeePerGas") or "0x0", 16)
            account_nonce = int(rpc_call(self.rpc, "eth_getTransactionCount", [self.address, "pending"], timeout=8), 16)
            try:
                priority_fee = int(rpc_call(self.rpc, "eth_maxPriorityFeePerGas", [], timeout=8), 16)
            except Exception:
                priority_fee = 0
            with self._lock:
                self.chain_id = chain_id
                self.base_fee = base_fee
                self.account_nonce = max(self.account_nonce, account_nonce)
                self.priority_fee = priority_fee
                self.last_update = time.time()
                self.last_error = ""
            return True
        except Exception as e:
            with self._lock:
                self.last_error = str(e)
            return False

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            self._refresh()

    def snapshot(self, min_priority_gwei: float, priority_gwei_override: Optional[float] = None) -> Optional[dict[str, int | float]]:
        now = time.time()
        with self._lock:
            age = now - self.last_update
            if self.chain_id <= 0 or self.base_fee <= 0 or self.last_update <= 0 or age > self.ttl:
                return None
            priority_floor = min_priority_gwei if priority_gwei_override is None else priority_gwei_override
            prio = max(self.priority_fee, Web3.to_wei(priority_floor, "gwei"))
            return {
                "chain_id": self.chain_id,
                "base_fee": self.base_fee,
                "account_nonce": self.account_nonce,
                "priority_fee": prio,
                "age": age,
            }

    def note_broadcast_nonce(self, nonce: int) -> None:
        with self._lock:
            if self.account_nonce <= nonce:
                self.account_nonce = nonce + 1


def default_backend() -> str:
    return os.getenv("HASH256_BACKEND") or ("cuda" if sys.platform.startswith("win") else "cpu")


def log(msg: str) -> None:
    print(time.strftime("[%H:%M:%S] ") + msg, flush=True)


def app_dir() -> Path:
    return Path(__file__).resolve().parent


def resource_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
    return app_dir()


def assets_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / "hash256_miner_assets"
    return app_dir() / "hash256_miner_assets"


_RPC_LOCAL = threading.local()


def rpc_urls(raw: str) -> list[str]:
    urls = [x.strip() for x in raw.split(",") if x.strip()]
    if not urls:
        raise RuntimeError("missing rpc url")
    return urls


def rpc_session() -> requests.Session:
    s = getattr(_RPC_LOCAL, "session", None)
    if s is None:
        s = requests.Session()
        _RPC_LOCAL.session = s
    return s


def rpc_call(url: str, method: str, params: list[Any], req_id: int = 1, timeout: int = 20) -> Any:
    last_error: Optional[Exception] = None
    for u in rpc_urls(url):
        try:
            r = rpc_session().post(
                u,
                json={"jsonrpc": "2.0", "id": req_id, "method": method, "params": params},
                timeout=timeout,
                headers={"Content-Type": "application/json", "User-Agent": "hash256-auto-miner/1.0", "Connection": "close"},
            )
            r.raise_for_status()
            j = r.json()
            if "error" in j:
                raise RuntimeError(f"{method}: {j['error']}")
            return j.get("result")
        except Exception as e:
            last_error = e
            with suppress(Exception):
                rpc_session().close()
            _RPC_LOCAL.session = None
            continue
    raise RuntimeError(f"{method}: all rpc failed: {last_error}")


def rpc_call_one(url: str, method: str, params: list[Any], req_id: int = 1, timeout: int = 20) -> Any:
    r = requests.post(
        url,
        json={"jsonrpc": "2.0", "id": req_id, "method": method, "params": params},
        timeout=timeout,
        headers={"Content-Type": "application/json", "User-Agent": "hash256-auto-miner/1.0"},
    )
    r.raise_for_status()
    j = r.json()
    if "error" in j:
        raise RuntimeError(f"{method}: {j['error']}")
    return j.get("result")


def rpc_error_message(e: Exception) -> str:
    cur: BaseException | None = e
    parts: list[str] = []
    while cur is not None:
        parts.append(str(cur))
        cur = cur.__cause__ or cur.__context__
    return " | ".join(parts).lower()


def is_tx_already_known(e: Exception) -> bool:
    msg = rpc_error_message(e)
    return any(x in msg for x in ("already known", "already imported", "known transaction", "nonce too low"))


def send_raw_transaction(rpc: str, raw: str, txh: str, broadcast_all: bool, timeout: int = 10) -> str:
    urls = rpc_urls(rpc)
    if not broadcast_all:
        return rpc_call(rpc, "eth_sendRawTransaction", [raw], timeout=timeout)

    ok = False
    errors: list[str] = []
    with ThreadPoolExecutor(max_workers=min(len(urls), 16)) as ex:
        futs = {ex.submit(rpc_call_one, u, "eth_sendRawTransaction", [raw], 1, timeout): u for u in urls}
        for fut in as_completed(futs):
            u = futs[fut]
            try:
                res = fut.result()
                if isinstance(res, str) and res.startswith("0x"):
                    ok = True
                    log(f"broadcast accepted {u}")
            except Exception as e:
                if is_tx_already_known(e):
                    ok = True
                    log(f"broadcast known {u}")
                else:
                    errors.append(f"{u}: {e}")
    if ok:
        return txh
    raise RuntimeError("all broadcast failed: " + " ; ".join(errors[-3:]))


def eth_call(url: str, to: str, data: str) -> str:
    return rpc_call(url, "eth_call", [{"to": to, "data": data}, "latest"])


def pad32_hex(x: int) -> str:
    return hex(x)[2:].rjust(64, "0")


def address_arg(addr: str) -> str:
    a = addr.lower().replace("0x", "")
    if len(a) != 40:
        raise ValueError("bad address")
    return a.rjust(64, "0")


def parse_uint_words(hex_data: str) -> list[int]:
    h = hex_data[2:] if hex_data.startswith("0x") else hex_data
    return [int(h[i:i + 64], 16) for i in range(0, len(h), 64) if len(h[i:i + 64]) == 64]


def get_challenge(rpc: str, contract: str, address: str) -> int:
    data = "0x" + SEL_GET_CHALLENGE + address_arg(address)
    return int(eth_call(rpc, contract, data), 16)


def get_mining_state(rpc: str, contract: str) -> dict[str, int]:
    raw = eth_call(rpc, contract, "0x" + SEL_MINING_STATE)
    w = parse_uint_words(raw)
    if len(w) < 7:
        raise RuntimeError(f"miningState decode failed: {raw}")
    return {
        "era": w[0],
        "reward": w[1],
        "difficulty": w[2],
        "minted": w[3],
        "remaining": w[4],
        "epoch": w[5],
        "blocks_to_next": w[6],
    }


def mine_data(nonce: int) -> str:
    return "0x" + SEL_MINE + pad32_hex(nonce)


def parse_nonce_value(raw: str) -> int:
    v = raw.strip()
    if not v:
        raise RuntimeError("missing nonce")
    return int(v, 16) if v.lower().startswith("0x") else int(v, 10)


def nonce_from_mine_data(data: str) -> int:
    if not data.startswith("0x" + SEL_MINE) or len(data) != 74:
        raise RuntimeError("not mine(uint256) calldata")
    return int(data[10:], 16)


def default_solutions_file() -> str:
    return os.getenv("HASH256_SOLUTIONS_FILE") or str(app_dir() / "hash256_solutions.jsonl")


def status_badge(status: str) -> str:
    return {
        "valid": "[valid]",
        "invalid": "[invalid]",
        "submitted": "[submitted]",
        "failed": "[failed]",
    }.get(status, f"[{status}]")


def save_solution_record(
    file_path: str,
    status: str,
    address: str,
    nonce: Optional[int] = None,
    result: Optional[str] = None,
    challenge: Optional[int] = None,
    difficulty: Optional[int] = None,
    epoch: Optional[int] = None,
    tx: str = "",
    error: str = "",
) -> None:
    if nonce is None:
        return
    p = Path(file_path)
    if not p.is_absolute():
        p = app_dir() / p
    p.parent.mkdir(parents=True, exist_ok=True)
    rec: dict[str, Any] = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "status": status,
        "badge": status_badge(status),
        "address": address,
        "nonce": "0x" + pad32_hex(nonce),
    }
    if result:
        rec["result"] = result
    if challenge is not None:
        rec["challenge"] = "0x" + pad32_hex(challenge)
    if difficulty is not None:
        rec["difficulty"] = "0x" + pad32_hex(difficulty)
    if epoch is not None:
        rec["epoch"] = epoch
    if tx:
        rec["tx"] = tx
    if error:
        rec["error"] = error
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n")
    log(f"{status_badge(status)} nonce=0x{nonce:064x} saved={p}")


def ensure_assets() -> None:
    d = assets_dir()
    d.mkdir(exist_ok=True)
    js = d / "hash_miner.js"
    wasm = d / "hash_miner_bg.wasm"
    pkg = d / "package.json"
    if not js.exists() or js.stat().st_size < 1000:
        log("下载 hash_miner.js")
        r = requests.get(f"{SITE}/miner/hash_miner.js", timeout=30)
        r.raise_for_status()
        js.write_text(r.text, encoding="utf-8")
    if not wasm.exists() or wasm.stat().st_size < 1000:
        log("下载 hash_miner_bg.wasm")
        r = requests.get(f"{SITE}/miner/hash_miner_bg.wasm", timeout=30)
        r.raise_for_status()
        wasm.write_bytes(r.content)
    if not pkg.exists():
        pkg.write_text('{"type":"module","private":true}\n', encoding="utf-8")
    write_node_worker()


def cuda_source_path() -> Path:
    return resource_dir() / "hash256_cuda_miner.cu"


def cuda_exe_path() -> Path:
    if getattr(sys, "frozen", False):
        external = Path(sys.executable).resolve().parent / "hash256_cuda_miner.exe"
        if external.exists():
            return external
        return resource_dir() / "hash256_cuda_miner.exe"
    return app_dir() / "hash256_cuda_miner.exe"


def cuda_driverapi_exe_path() -> Path:
    if getattr(sys, "frozen", False):
        external = Path(sys.executable).resolve().parent / "hash256_cuda_miner_driverapi.exe"
        if external.exists():
            return external
        return resource_dir() / "hash256_cuda_miner_driverapi.exe"
    return app_dir() / "hash256_cuda_miner_driverapi.exe"


def find_vcvars64() -> Optional[Path]:
    candidates = [
        Path(r"C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvars64.bat"),
        Path(r"C:\Program Files\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvars64.bat"),
        Path(r"C:\Program Files\Microsoft Visual Studio\2022\Community\VC\Auxiliary\Build\vcvars64.bat"),
        Path(r"C:\Program Files\Microsoft Visual Studio\2022\Professional\VC\Auxiliary\Build\vcvars64.bat"),
    ]
    for p in candidates:
        if p.exists():
            return p
    return None


def run_nvcc_compile(cmd: list[str]) -> subprocess.CompletedProcess:
    p = subprocess.run(cmd, cwd=str(app_dir()), text=True, capture_output=True)
    if p.returncode == 0 or "Cannot find compiler 'cl.exe'" not in (p.stderr or p.stdout):
        return p
    vcvars = find_vcvars64()
    if not vcvars:
        return p
    cmdline = 'call ""{}"" >nul && {}'.format(vcvars, subprocess.list2cmdline(cmd))
    return subprocess.run(["cmd", "/c", cmdline], cwd=str(app_dir()), text=True, capture_output=True)


def ensure_cuda_miner() -> Path:
    src = cuda_source_path()
    exe = cuda_exe_path()
    if getattr(sys, "frozen", False):
        if exe.exists():
            return exe
        raise RuntimeError("打包文件缺少 hash256_cuda_miner.exe")

    nvcc = shutil.which("nvcc")
    if not nvcc:
        if exe.exists():
            return exe
        raise RuntimeError("nvcc not found")
    if not src.exists():
        raise RuntimeError(f"missing {src}")
    if exe.exists() and exe.stat().st_mtime >= src.stat().st_mtime:
        return exe

    log("compiling CUDA miner")
    fatbin_arch = [
        "-gencode=arch=compute_75,code=sm_75",
        "-gencode=arch=compute_86,code=sm_86",
        "-gencode=arch=compute_89,code=sm_89",
        "-gencode=arch=compute_120,code=sm_120",
        "-gencode=arch=compute_120,code=compute_120",
    ]
    cmd = [nvcc, "-O3", "-std=c++17", *fatbin_arch, src.name, "-o", exe.name]
    p = run_nvcc_compile(cmd)
    if p.returncode != 0:
        cmd = [nvcc, "-O3", "-std=c++17", src.name, "-o", exe.name]
        p = run_nvcc_compile(cmd)
    if p.returncode != 0:
        if exe.exists():
            log("CUDA compile failed, using bundled hash256_cuda_miner.exe")
            return exe
        raise RuntimeError((p.stderr or p.stdout or "nvcc compile failed").strip())
    return exe


def ensure_cuda_driverapi_miner() -> Optional[Path]:
    exe = cuda_driverapi_exe_path()
    return exe if exe.exists() else None


def write_node_worker() -> Path:
    p = assets_dir() / "hash256_wasm_worker.mjs"
    p.write_text(
        r'''
import fs from "node:fs";
import { Miner, initSync } from "./hash_miner.js";

function hexToBytes(hex) {
  hex = String(hex).replace(/^0x/, "");
  if (hex.length % 2) hex = "0" + hex;
  return Uint8Array.from(Buffer.from(hex, "hex"));
}

function bytesToHex(bytes) {
  return "0x" + Buffer.from(bytes).toString("hex");
}

function sleep(ms) {
  return new Promise(r => setTimeout(r, ms));
}

const cfg = JSON.parse(process.argv[2]);
initSync(fs.readFileSync(new URL("./hash_miner_bg.wasm", import.meta.url)));

const challenge = hexToBytes(cfg.challenge);
const difficulty = hexToBytes(cfg.difficulty);
const prefix = hexToBytes(cfg.prefix);
const batch = BigInt(cfg.batch || 1000000);
const progressMs = Number(cfg.progressMs || 2500);
const id = Number(cfg.id || 0);

let miner = new Miner(challenge, difficulty, prefix);
let counter = 0n;
let total = 0n;
let lastAt = Date.now();
let lastTotal = 0n;

while (true) {
  const hit = miner.search(counter, batch);
  counter += batch;
  total += batch;

  if (hit) {
    console.log(JSON.stringify({
      type: "found",
      id,
      nonce: bytesToHex(hit.nonce),
      result: bytesToHex(hit.result),
      hashes: hit.hashes.toString(),
      total: total.toString()
    }));
    process.exit(0);
  }

  const now = Date.now();
  if (now - lastAt >= progressMs) {
    const delta = total - lastTotal;
    const hps = Number(delta) / ((now - lastAt) / 1000);
    console.log(JSON.stringify({ type: "progress", id, total: total.toString(), hps }));
    lastAt = now;
    lastTotal = total;
    await sleep(0);
  }
}
'''.lstrip(),
        encoding="utf-8",
    )
    return p


def nonce_prefix_hex(zero_prefix: bool) -> str:
    return "00" * 24 if zero_prefix else secrets.token_hex(24)


def choose_cuda_exe_and_devices(device: int, devices_arg: str) -> tuple[Path, list[int]]:
    try:
        exe = ensure_cuda_miner()
    except Exception as e:
        fallback = ensure_cuda_driverapi_miner()
        if not fallback:
            raise
        log(f"cuda runtime miner unavailable ({e}), use driverapi fallback: {fallback.name}")
        exe = fallback
    try:
        devices = list_cuda_devices(exe)
    except Exception as e:
        log(f"cuda list failed on {exe.name}: {e}")
        fallback = ensure_cuda_driverapi_miner()
        if not fallback:
            raise
        log(f"cuda runtime unavailable, try driverapi fallback: {fallback.name}")
        exe = fallback
        devices = list_cuda_devices(exe)

    if devices_arg:
        raw = devices_arg.strip().lower()
        if raw == "all":
            ids = [int(d["id"]) for d in devices]
        else:
            ids = [int(x.strip()) for x in raw.split(",") if x.strip()]
    elif device < 0:
        best = max(devices, key=lambda d: int(d.get("score") or 0))
        ids = [int(best["id"])]
    else:
        ids = [device]

    known = {int(d["id"]): d for d in devices}
    selected = []
    for did in ids:
        if did not in known:
            raise RuntimeError(f"cuda device {did} not found")
        selected.append(did)
        d = known[did]
        log(
            "cuda select exe={exe} device={id} {name} cc={major}.{minor} sm={mp} mem={mem_gb:.1f}GB".format(
                exe=exe.name,
                id=d.get("id"),
                name=d.get("name"),
                major=d.get("major"),
                minor=d.get("minor"),
                mp=d.get("multiProcessorCount"),
                mem_gb=float(d.get("totalGlobalMem") or 0) / (1024 ** 3),
            )
        )
    return exe, selected


def start_cuda_worker(
    challenge: int,
    difficulty: int,
    batch: int,
    blocks: int,
    threads: int,
    device: int,
    devices_arg: str,
    zero_prefix: bool,
) -> tuple[list[subprocess.Popen], queue.Queue]:
    try:
        exe, device_ids = choose_cuda_exe_and_devices(device, devices_arg)
    except Exception:
        log_cuda_runtime_hint()
        raise
    q: queue.Queue = queue.Queue()
    procs: list[subprocess.Popen] = []

    for worker_id, actual_device in enumerate(device_ids):
        prefix = nonce_prefix_hex(zero_prefix)
        p = subprocess.Popen(
            [
                str(exe),
                "0x" + pad32_hex(challenge),
                "0x" + pad32_hex(difficulty),
                "0x" + prefix,
                str(batch),
                str(blocks),
                str(threads),
                str(actual_device),
            ],
            cwd=str(exe.parent),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=1,
        )
        procs.append(p)

        def reader(proc: subprocess.Popen, idx: int, dev: int) -> None:
            assert proc.stdout is not None
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                    msg.setdefault("id", idx)
                    msg.setdefault("device", dev)
                    q.put(msg)
                except Exception:
                    q.put({"type": "log", "id": idx, "device": dev, "line": line})

        def err_reader(proc: subprocess.Popen, idx: int, dev: int) -> None:
            assert proc.stderr is not None
            for line in proc.stderr:
                line = line.strip()
                if line:
                    q.put({"type": "err", "id": idx, "device": dev, "line": line})

        threading.Thread(target=reader, args=(p, worker_id, actual_device), daemon=True).start()
        threading.Thread(target=err_reader, args=(p, worker_id, actual_device), daemon=True).start()

    return procs, q


def list_cuda_devices(exe: Path) -> list[dict[str, Any]]:
    p = subprocess.run(
        [str(exe), "--list"],
        cwd=str(exe.parent),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
    )
    if p.returncode != 0:
        msg = (p.stderr or p.stdout or "").strip()
        raise RuntimeError(msg or "cuda device list failed")
    info = json.loads((p.stdout or "").strip() or "{}")
    devices = info.get("devices") or []
    if not devices:
        raise RuntimeError("no cuda devices")
    return devices


def try_auto_cuda_device(exe: Path) -> tuple[bool, int]:
    try:
        devices = list_cuda_devices(exe)
        best = max(devices, key=lambda d: int(d.get("score") or 0))
        log(
            "cuda auto exe={exe} device={id} {name} cc={major}.{minor} sm={mp} mem={mem_gb:.1f}GB".format(
                exe=exe.name,
                id=best.get("id"),
                name=best.get("name"),
                major=best.get("major"),
                minor=best.get("minor"),
                mp=best.get("multiProcessorCount"),
                mem_gb=float(best.get("totalGlobalMem") or 0) / (1024 ** 3),
            )
        )
        return True, int(best["id"])
    except Exception as e:
        log(f"cuda auto device failed on {exe.name}: {e}")
        return False, 0


def auto_cuda_device(exe: Path) -> int:
    ok, device = try_auto_cuda_device(exe)
    if ok:
        return device
    log_cuda_runtime_hint()
    log("cuda auto device failed, fallback device=0")
    return 0


def log_cuda_runtime_hint() -> None:
    try:
        p = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,driver_version,compute_cap,driver_model.current",
                "--format=csv,noheader",
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=8,
        )
        out = (p.stdout or p.stderr or "").strip()
        if out:
            for line in out.splitlines():
                log(f"nvidia-smi {line.strip()}")
    except Exception as e:
        log(f"nvidia-smi unavailable: {e}")


def start_workers(challenge: int, difficulty: int, threads: int, batch: int, zero_prefix: bool) -> tuple[list[subprocess.Popen], queue.Queue]:
    node = shutil.which("node")
    if not node:
        raise RuntimeError("未找到 node")
    ensure_assets()
    worker = assets_dir() / "hash256_wasm_worker.mjs"
    q: queue.Queue = queue.Queue()
    procs: list[subprocess.Popen] = []

    for i in range(threads):
        prefix = nonce_prefix_hex(zero_prefix)
        cfg = {
            "id": i,
            "challenge": "0x" + pad32_hex(challenge),
            "difficulty": "0x" + pad32_hex(difficulty),
            "prefix": "0x" + prefix,
            "batch": batch,
            "progressMs": 2500,
        }
        p = subprocess.Popen(
            [node, str(worker), json.dumps(cfg)],
            cwd=str(assets_dir()),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=1,
        )
        procs.append(p)

        def reader(proc: subprocess.Popen, idx: int) -> None:
            assert proc.stdout is not None
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    q.put(json.loads(line))
                except Exception:
                    q.put({"type": "log", "id": idx, "line": line})

        def err_reader(proc: subprocess.Popen, idx: int) -> None:
            assert proc.stderr is not None
            for line in proc.stderr:
                line = line.strip()
                if line:
                    q.put({"type": "err", "id": idx, "line": line})

        threading.Thread(target=reader, args=(p, i), daemon=True).start()
        threading.Thread(target=err_reader, args=(p, i), daemon=True).start()

    return procs, q


def stop_workers(procs: list[subprocess.Popen]) -> None:
    for p in procs:
        if p.poll() is None:
            p.terminate()
    time.sleep(0.5)
    for p in procs:
        if p.poll() is None:
            p.kill()


def find_nonce(challenge: int, difficulty: int, threads: int, batch: int) -> int:
    procs, q = start_workers(challenge, difficulty, threads, batch, False)
    totals: dict[int, int] = {}
    rates: dict[int, float] = {}
    started = time.time()
    try:
        while True:
            msg = q.get(timeout=5)
            typ = msg.get("type")
            if typ == "found":
                stop_workers(procs)
                nonce = int(str(msg["nonce"]).replace("0x", ""), 16)
                result = msg.get("result")
                log(f"命中 nonce=0x{nonce:064x} result={result}")
                return nonce
            if typ == "progress":
                idx = int(msg["id"])
                totals[idx] = int(msg["total"])
                rates[idx] = float(msg["hps"])
                total = sum(totals.values())
                rate = sum(rates.values())
                elapsed = time.time() - started
                log(f"搜索中 {rate/1e6:.2f} MH/s · {threads} 线程 · {total:,} hashes · {elapsed:.1f}s")
            elif typ in ("err", "log"):
                log(f"worker {msg.get('id')}: {msg.get('line')}")
    finally:
        stop_workers(procs)


def read_target(rpc: str, contract: str, address: str) -> tuple[dict[str, int], int, int]:
    state = get_mining_state(rpc, contract)
    challenge = get_challenge(rpc, contract, address)
    return state, challenge, state["difficulty"]


def find_nonce_live(
    rpc: str,
    contract: str,
    address: str,
    backend: str,
    threads: int,
    batch: int,
    cuda_batch: int,
    cuda_blocks: int,
    cuda_threads: int,
    cuda_device: int,
    cuda_devices: str,
    poll_interval: float,
    skip_final_check: bool,
    submit_on_final_check_fail: bool,
    zero_prefix: bool,
    solutions_file: str,
) -> tuple[int, dict[str, int], int, int]:
    state, challenge, difficulty = read_target(rpc, contract, address)
    procs: list[subprocess.Popen] = []
    q: queue.Queue = queue.Queue()
    totals: dict[int, int] = {}
    rates: dict[int, float] = {}
    started = time.time()
    last_poll = 0.0

    def restart(new_state: dict[str, int], new_challenge: int, new_difficulty: int) -> None:
        nonlocal procs, q, totals, rates, started, state, challenge, difficulty
        stop_workers(procs)
        state, challenge, difficulty = new_state, new_challenge, new_difficulty
        log(f"epoch={state['epoch']} reward={state['reward'] // 10**18} HASH minted={state['minted'] // 10**18} HASH")
        log(f"challenge=0x{challenge:064x}")
        log(f"difficulty=0x{difficulty:064x}")
        log(f"nonce_prefix={'zero' if zero_prefix else 'random'}")
        if backend == "cuda":
            procs, q = start_cuda_worker(challenge, difficulty, cuda_batch, cuda_blocks, cuda_threads, cuda_device, cuda_devices, zero_prefix)
            dev_label = cuda_devices or str(cuda_device)
            log(f"backend=cuda devices={dev_label} workers={len(procs)} blocks={cuda_blocks} threads={cuda_threads} batch={cuda_batch:,}")
        else:
            procs, q = start_workers(challenge, difficulty, threads, batch, zero_prefix)
            log(f"backend=cpu workers={threads} batch={batch:,}")
        totals = {}
        rates = {}
        started = time.time()

    try:
        restart(state, challenge, difficulty)
        while True:
            now = time.time()
            if now - last_poll >= poll_interval:
                last_poll = now
                try:
                    new_state, new_challenge, new_difficulty = read_target(rpc, contract, address)
                except Exception as e:
                    log(f"poll target failed, keep mining: {e}")
                    continue
                if (
                    new_state["epoch"] != state["epoch"]
                    or new_challenge != challenge
                    or new_difficulty != difficulty
                ):
                    log("target changed, restart mining")
                    restart(new_state, new_challenge, new_difficulty)
                    continue

            try:
                msg = q.get(timeout=0.5)
            except queue.Empty:
                continue

            typ = msg.get("type")
            if typ == "found":
                nonce = int(str(msg["nonce"]).replace("0x", ""), 16)
                result = msg.get("result")
                if not skip_final_check:
                    try:
                        new_state, new_challenge, new_difficulty = read_target(rpc, contract, address)
                        if (
                            new_state["epoch"] != state["epoch"]
                            or new_challenge != challenge
                            or new_difficulty != difficulty
                        ):
                            if submit_on_final_check_fail:
                                log("hit may be stale, aggressive submit anyway")
                            else:
                                save_solution_record(
                                    solutions_file,
                                    "invalid",
                                    address,
                                    nonce,
                                    str(result) if result else None,
                                    challenge,
                                    difficulty,
                                    state.get("epoch"),
                                    error="target changed before submit",
                                )
                                log("hit became stale before submit, discard and restart")
                                restart(new_state, new_challenge, new_difficulty)
                                continue
                    except Exception as e:
                        if submit_on_final_check_fail:
                            log(f"final target check failed, aggressive submit anyway: {e}")
                        else:
                            save_solution_record(
                                solutions_file,
                                "invalid",
                                address,
                                nonce,
                                str(result) if result else None,
                                challenge,
                                difficulty,
                                state.get("epoch"),
                                error=f"final target check failed: {e}",
                            )
                            log(f"final target check failed, discard hit: {e}")
                            continue
                stop_workers(procs)
                log(f"found nonce=0x{nonce:064x} result={result}")
                return nonce, state, challenge, difficulty

            if typ == "progress":
                idx = int(msg["id"])
                totals[idx] = int(msg["total"])
                rates[idx] = float(msg["hps"])
                total = sum(totals.values())
                rate = sum(rates.values())
                elapsed = time.time() - started
                log(f"mining {rate/1e6:.2f} MH/s | {backend} | {total:,} hashes | {elapsed:.1f}s")
            elif typ in ("err", "log"):
                line = str(msg.get("line", ""))
                if "deprecated" not in line.lower():
                    log(f"worker {msg.get('id')}: {line}")
    finally:
        stop_workers(procs)


def build_and_send(
    rpc: str,
    contract: str,
    private_key: str,
    data: str,
    gas_multiplier: float,
    send_on_estimate_fail: bool,
    min_priority_gwei: float,
    max_fee_base_multiplier: float,
    fallback_gas: int,
    max_gas: int,
    estimate_gas: bool,
    broadcast_all_rpcs: bool,
    fixed_nonce: Optional[int] = None,
    priority_gwei_override: Optional[float] = None,
    chain_cache: Optional[ChainCache] = None,
) -> tuple[str, int]:
    acct = Account.from_key(private_key)
    from_addr = acct.address
    contract = to_checksum_address(contract)
    snap = None if estimate_gas or chain_cache is None else chain_cache.snapshot(min_priority_gwei, priority_gwei_override)
    if snap is not None:
        chain_id = int(snap["chain_id"])
        tx_count = fixed_nonce if fixed_nonce is not None else int(snap["account_nonce"])
        base_fee = int(snap["base_fee"])
        prio = int(snap["priority_fee"])
        log(f"using chain cache age={float(snap['age']):.3f}s")
    else:
        if chain_cache is not None and not estimate_gas:
            log("chain cache stale, fallback rpc signing")
        chain_id = int(rpc_call(rpc, "eth_chainId", []), 16)
        tx_count = fixed_nonce
        if tx_count is None:
            tx_count = int(rpc_call(rpc, "eth_getTransactionCount", [from_addr, "pending"]), 16)
        latest = rpc_call(rpc, "eth_getBlockByNumber", ["latest", False])
        base_fee = int(latest.get("baseFeePerGas") or "0x0", 16)
        try:
            prio = int(rpc_call(rpc, "eth_maxPriorityFeePerGas", []), 16)
        except Exception:
            prio = 0
        effective_priority = min_priority_gwei if priority_gwei_override is None else priority_gwei_override
        prio = max(prio, Web3.to_wei(effective_priority, "gwei"))
    max_fee = max(int(base_fee * max_fee_base_multiplier) + prio, prio)
    tx_obj = {"from": from_addr, "to": contract, "data": data, "value": "0x0"}
    if estimate_gas:
        try:
            gas_est = int(rpc_call(rpc, "eth_estimateGas", [tx_obj]), 16)
        except Exception as e:
            if not send_on_estimate_fail:
                raise RuntimeError(f"估算 gas 失败，未广播: {e}") from e
            log(f"估算 gas 失败，使用 fallback gas={fallback_gas} 继续广播: {e}")
            gas_est = fallback_gas
            gas_multiplier = 1.0
    else:
        gas_est = fallback_gas
        gas_multiplier = 1.0
    gas = int(gas_est * gas_multiplier)
    gas = max(200000, min(max_gas, gas))
    tx = {
        "chainId": chain_id,
        "nonce": tx_count,
        "to": contract,
        "value": 0,
        "data": data,
        "gas": gas,
        "maxFeePerGas": max_fee,
        "maxPriorityFeePerGas": prio,
        "type": 2,
    }
    log(
        "签名广播 "
        f"from={from_addr} nonce={tx_count} gas={gas} "
        f"maxFee={float(Web3.from_wei(max_fee, 'gwei')):.6f}gwei "
        f"prio={float(Web3.from_wei(prio, 'gwei')):.6f}gwei"
    )
    signed = acct.sign_transaction(tx)
    raw = signed.raw_transaction.hex()
    if not raw.startswith("0x"):
        raw = "0x" + raw
    txh = signed.hash.hex()
    if not txh.startswith("0x"):
        txh = "0x" + txh
    sent_txh = send_raw_transaction(rpc, raw, txh, broadcast_all_rpcs)
    if chain_cache is not None:
        chain_cache.note_broadcast_nonce(tx_count)
    return sent_txh, tx_count


def wait_tx_receipt(rpc: str, txh: str, timeout: float, interval: float) -> Optional[dict[str, Any]]:
    deadline = time.time() + max(1.0, timeout)
    while time.time() < deadline:
        try:
            receipt = rpc_call(rpc, "eth_getTransactionReceipt", [txh])
            if receipt:
                return receipt
        except Exception as e:
            log(f"receipt poll failed: {e}")
        time.sleep(max(0.5, interval))
    return None


def build_send_confirm_retry(
    rpc: str,
    contract: str,
    private_key: str,
    data: str,
    gas_multiplier: float,
    send_on_estimate_fail: bool,
    min_priority_gwei: float,
    max_fee_base_multiplier: float,
    fallback_gas: int,
    max_gas: int,
    estimate_gas: bool,
    resend_on_fail: int,
    receipt_timeout: float,
    receipt_interval: float,
    broadcast_all_rpcs: bool,
    replace_pending: int,
    replace_bump: float,
    chain_cache: Optional[ChainCache] = None,
) -> str:
    last_txh = ""
    attempts = max(1, resend_on_fail + 1)
    for attempt in range(1, attempts + 1):
        txh, tx_nonce = build_and_send(
            rpc,
            contract,
            private_key,
            data,
            gas_multiplier,
            send_on_estimate_fail,
            min_priority_gwei,
            max_fee_base_multiplier,
            fallback_gas,
            max_gas,
            estimate_gas,
            broadcast_all_rpcs,
            chain_cache=chain_cache,
        )
        last_txh = txh
        log(f"broadcast ok attempt={attempt}/{attempts} tx={txh}")
        receipt = wait_tx_receipt(rpc, txh, receipt_timeout, receipt_interval)
        replace_priority = min_priority_gwei
        for replace_i in range(1, max(0, replace_pending) + 1):
            if receipt is not None:
                break
            replace_priority *= max(1.101, replace_bump)
            log(f"receipt timeout, speed up same nonce={tx_nonce} prio={replace_priority:.3f}gwei")
            txh, _ = build_and_send(
                rpc,
                contract,
                private_key,
                data,
                gas_multiplier,
                send_on_estimate_fail,
                min_priority_gwei,
                max_fee_base_multiplier,
                fallback_gas,
                max_gas,
                estimate_gas,
                broadcast_all_rpcs,
                fixed_nonce=tx_nonce,
                priority_gwei_override=replace_priority,
                chain_cache=chain_cache,
            )
            last_txh = txh
            log(f"speedup broadcast ok {replace_i}/{replace_pending} tx={txh}")
            receipt = wait_tx_receipt(rpc, txh, receipt_timeout, receipt_interval)
        if receipt is None:
            log(f"receipt timeout, tx may still pending, stop resend tx={txh}")
            return txh

        status = int(receipt.get("status") or "0x0", 16)
        gas_used = int(receipt.get("gasUsed") or "0x0", 16)
        block = int(receipt.get("blockNumber") or "0x0", 16)
        if status == 1:
            log(f"tx success block={block} gasUsed={gas_used} tx={txh}")
            return txh

        log(f"tx failed block={block} gasUsed={gas_used} tx={txh}")
        if attempt < attempts:
            log("resend same calldata with next account nonce")
            time.sleep(1.0)

    return last_txh


def post_hit(submit_to: str, token: str, address: str, data: str, nonce: int) -> str:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Submit-Token"] = token
    r = requests.post(
        submit_to,
        json={"address": address, "data": data, "nonce": f"0x{nonce:064x}"},
        headers=headers,
        timeout=10,
    )
    r.raise_for_status()
    j = r.json()
    if not j.get("ok"):
        raise RuntimeError(j.get("error") or "submitter rejected hit")
    return str(j.get("tx") or "")


def run_submit_server(args: argparse.Namespace, private_key: str, address: str, contract: str, chain_cache: Optional[ChainCache]) -> None:
    submit_lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *vals: Any) -> None:
            return

        def _json(self, status: int, body: dict[str, Any]) -> None:
            raw = json.dumps(body, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self) -> None:
            if self.path.rstrip("/") == "/health":
                self._json(200, {"ok": True, "address": address})
            else:
                self._json(404, {"ok": False, "error": "not found"})

        def do_POST(self) -> None:
            if self.path.rstrip("/") != "/submit":
                self._json(404, {"ok": False, "error": "not found"})
                return
            if args.submit_token and self.headers.get("X-Submit-Token") != args.submit_token:
                self._json(403, {"ok": False, "error": "bad token"})
                return
            try:
                size = int(self.headers.get("Content-Length") or "0")
                body = self.rfile.read(min(size, 4096))
                payload = json.loads(body.decode("utf-8"))
                data = str(payload.get("data") or "").strip()
                if not data:
                    raw_nonce = str(payload.get("nonce") or "").replace("0x", "")
                    if not raw_nonce:
                        raise RuntimeError("missing data or nonce")
                    data = mine_data(int(raw_nonce, 16))
                if not data.startswith("0x" + SEL_MINE) or len(data) != 74:
                    raise RuntimeError("bad mine calldata")
                with submit_lock:
                    txh, tx_nonce = build_and_send(
                        args.rpc,
                        contract,
                        private_key,
                        data,
                        args.gas_multiplier,
                        not args.no_send_on_estimate_fail,
                        args.min_priority_gwei,
                        args.max_fee_base_multiplier,
                        args.fallback_gas,
                        args.max_gas,
                        args.estimate_gas,
                        args.broadcast_all_rpcs,
                        chain_cache=chain_cache,
                    )
                log(f"submitter broadcast client={self.client_address[0]} nonce={tx_nonce} tx={txh}")
                self._json(200, {"ok": True, "tx": txh, "nonce": tx_nonce})
            except Exception as e:
                log(f"submitter rejected hit from={self.client_address[0]} error={e}")
                self._json(500, {"ok": False, "error": str(e)})

    server = ThreadingHTTPServer((args.submit_host, args.submit_port), Handler)
    log(f"submitter listening http://{args.submit_host}:{args.submit_port}/submit address={address}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        server.server_close()


def clean_pk(pk: str) -> str:
    pk = pk.strip()
    if not pk:
        raise RuntimeError("缺少私钥")
    if not pk.startswith("0x"):
        pk = "0x" + pk
    if len(pk) != 66:
        raise RuntimeError("私钥长度不正确")
    return pk


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="HASH256 terminal auto miner + broadcaster")
    ap.add_argument("--rpc", default=os.getenv("HASH256_RPC", RPC_URL))
    ap.add_argument("--contract", default=CONTRACT)
    ap.add_argument("--private-key", default=os.getenv("HASH256_PRIVATE_KEY", ""))
    ap.add_argument("--backend", choices=("cuda", "cpu"), default=default_backend())
    ap.add_argument("--threads", type=int, default=max(1, min(8, (os.cpu_count() or 4) - 1)))
    ap.add_argument("--batch", type=int, default=1_000_000)
    ap.add_argument("--cuda-batch", type=int, default=16_777_216)
    ap.add_argument("--cuda-blocks", type=int, default=4096)
    ap.add_argument("--cuda-threads", type=int, default=256)
    ap.add_argument("--cuda-device", type=int, default=-1, help="-1 自动选择算力最高的 CUDA 显卡")
    ap.add_argument("--cuda-devices", default="", help="CUDA device list, e.g. all or 0,1,2,3. Overrides --cuda-device")
    ap.add_argument("--poll-interval", type=float, default=3.0)
    ap.add_argument("--gas-multiplier", type=float, default=1.5)
    ap.add_argument("--min-priority-gwei", type=float, default=5.0)
    ap.add_argument("--max-fee-base-multiplier", type=float, default=4.0)
    ap.add_argument("--fallback-gas", type=int, default=400000)
    ap.add_argument("--max-gas", type=int, default=500000)
    ap.add_argument("--estimate-gas", action="store_true")
    ap.add_argument("--no-send-on-estimate-fail", action="store_true")
    ap.add_argument("--skip-final-check", action="store_true")
    ap.add_argument("--submit-on-final-check-fail", action="store_true")
    ap.add_argument("--zero-prefix", action="store_true")
    ap.add_argument("--resend-on-fail", type=int, default=1, help="resend same calldata when receipt status is 0")
    ap.add_argument("--receipt-timeout", type=float, default=90.0, help="seconds to wait for each transaction receipt")
    ap.add_argument("--receipt-interval", type=float, default=3.0, help="seconds between receipt polling calls")
    ap.add_argument("--broadcast-all-rpcs", action="store_true", help="send the same signed tx to every RPC in --rpc concurrently")
    ap.add_argument("--replace-pending", type=int, default=0, help="speed up pending tx with the same account nonce this many times")
    ap.add_argument("--replace-bump", type=float, default=1.35, help="priority fee multiplier for each pending speedup")
    ap.add_argument("--no-chain-cache", action="store_true", help="disable background chain state cache before signing")
    ap.add_argument("--chain-cache-interval", type=float, default=0.5, help="seconds between background chain state refreshes")
    ap.add_argument("--chain-cache-ttl", type=float, default=2.0, help="max cache age allowed for zero-rpc signing")
    ap.add_argument("--submit-nonce", default="", help="retry submit a saved nonce, hex or decimal")
    ap.add_argument("--solutions-file", default=default_solutions_file(), help="append nonce status records as JSONL")
    ap.add_argument("--submit-data", default="", help="直接广播已命中的 calldata，例如 0x4d474898...")
    ap.add_argument("--loop", action="store_true", help="广播成功后继续挖下一次")
    return ap.parse_args()


def main() -> None:
    args = parse_args()
    pk = clean_pk(args.private_key or getpass.getpass("输入私钥(隐藏，不会保存): "))
    acct = Account.from_key(pk)
    addr = acct.address
    contract = to_checksum_address(args.contract)
    log(f"钱包 {addr}")
    log(f"合约 {contract}")

    chain_cache: Optional[ChainCache] = None
    if not args.no_chain_cache and not args.estimate_gas:
        chain_cache = ChainCache(args.rpc, addr, args.chain_cache_interval, args.chain_cache_ttl)
        if not chain_cache.start():
            chain_cache = None

    while True:
        current_nonce: Optional[int] = None
        current_result: Optional[str] = None
        current_state: Optional[dict[str, int]] = None
        current_challenge: Optional[int] = None
        current_difficulty: Optional[int] = None
        try:
            if args.submit_nonce:
                current_nonce = parse_nonce_value(args.submit_nonce)
                data = mine_data(current_nonce)
                log(f"retry submit nonce=0x{current_nonce:064x}")
            elif args.submit_data:
                data = args.submit_data.strip()
                current_nonce = nonce_from_mine_data(data)
                if not data.startswith("0x" + SEL_MINE) or len(data) != 74:
                    raise RuntimeError("submit-data 不是 mine(uint256) calldata")
                log(f"直接提交 data={data}")
            else:
                nonce, state, challenge, difficulty = find_nonce_live(
                    args.rpc,
                    contract,
                    addr,
                    args.backend,
                    args.threads,
                    args.batch,
                    args.cuda_batch,
                    args.cuda_blocks,
                    args.cuda_threads,
                    args.cuda_device,
                    args.cuda_devices,
                    args.poll_interval,
                    args.skip_final_check,
                    args.submit_on_final_check_fail,
                    args.zero_prefix,
                    args.solutions_file,
                )
                current_nonce = nonce
                current_state = state
                current_challenge = challenge
                current_difficulty = difficulty
                data = mine_data(nonce)
                save_solution_record(
                    args.solutions_file,
                    "valid",
                    addr,
                    nonce,
                    current_result,
                    challenge,
                    difficulty,
                    state.get("epoch"),
                )
                log(f"calldata={data}")

            txh = build_send_confirm_retry(
                args.rpc,
                contract,
                pk,
                data,
                args.gas_multiplier,
                not args.no_send_on_estimate_fail,
                args.min_priority_gwei,
                args.max_fee_base_multiplier,
                args.fallback_gas,
                args.max_gas,
                args.estimate_gas,
                args.resend_on_fail,
                args.receipt_timeout,
                args.receipt_interval,
                args.broadcast_all_rpcs,
                args.replace_pending,
                args.replace_bump,
                chain_cache,
            )

            log(f"final tx={txh}")
            save_solution_record(
                args.solutions_file,
                "submitted",
                addr,
                current_nonce,
                current_result,
                current_challenge,
                current_difficulty,
                current_state.get("epoch") if current_state else None,
                tx=txh,
            )
            print(txh)

            if args.submit_data or args.submit_nonce or not args.loop:
                return
            time.sleep(2)
        except KeyboardInterrupt:
            print("\n已停止")
            if chain_cache is not None:
                chain_cache.stop()
            return
        except Exception as e:
            log(f"失败: {e}")
            if args.submit_data or not args.loop:
                raise SystemExit(2)
            time.sleep(3)


if __name__ == "__main__":
    main()
