#include <cuda_runtime.h>

#include <atomic>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <iostream>
#include <sstream>
#include <string>
#include <thread>
#include <vector>

__device__ __constant__ uint64_t KECCAKF_RNDC[24] = {
    0x0000000000000001ULL, 0x0000000000008082ULL, 0x800000000000808aULL,
    0x8000000080008000ULL, 0x000000000000808bULL, 0x0000000080000001ULL,
    0x8000000080008081ULL, 0x8000000000008009ULL, 0x000000000000008aULL,
    0x0000000000000088ULL, 0x0000000080008009ULL, 0x000000008000000aULL,
    0x000000008000808bULL, 0x800000000000008bULL, 0x8000000000008089ULL,
    0x8000000000008003ULL, 0x8000000000008002ULL, 0x8000000000000080ULL,
    0x000000000000800aULL, 0x800000008000000aULL, 0x8000000080008081ULL,
    0x8000000000008080ULL, 0x0000000080000001ULL, 0x8000000080008008ULL};

__device__ __constant__ int KECCAKF_ROTC[24] = {
    1, 3, 6, 10, 15, 21, 28, 36, 45, 55, 2, 14,
    27, 41, 56, 8, 25, 43, 62, 18, 39, 61, 20, 44};

__device__ __constant__ int KECCAKF_PILN[24] = {
    10, 7, 11, 17, 18, 3, 5, 16, 8, 21, 24, 4,
    15, 23, 19, 13, 12, 2, 20, 14, 22, 9, 6, 1};

__device__ __forceinline__ uint64_t rotl64(uint64_t x, int s) {
    return (x << s) | (x >> (64 - s));
}

__device__ void keccakf(uint64_t st[25]) {
    uint64_t bc[5];
    for (int round = 0; round < 24; round++) {
        for (int i = 0; i < 5; i++) {
            bc[i] = st[i] ^ st[i + 5] ^ st[i + 10] ^ st[i + 15] ^ st[i + 20];
        }
        for (int i = 0; i < 5; i++) {
            uint64_t t = bc[(i + 4) % 5] ^ rotl64(bc[(i + 1) % 5], 1);
            for (int j = 0; j < 25; j += 5) st[j + i] ^= t;
        }

        uint64_t t = st[1];
        for (int i = 0; i < 24; i++) {
            int j = KECCAKF_PILN[i];
            bc[0] = st[j];
            st[j] = rotl64(t, KECCAKF_ROTC[i]);
            t = bc[0];
        }

        for (int j = 0; j < 25; j += 5) {
            for (int i = 0; i < 5; i++) bc[i] = st[j + i];
            for (int i = 0; i < 5; i++) st[j + i] ^= (~bc[(i + 1) % 5]) & bc[(i + 2) % 5];
        }
        st[0] ^= KECCAKF_RNDC[round];
    }
}

__device__ __forceinline__ uint64_t load64_le(const uint8_t* p) {
    uint64_t v = 0;
    #pragma unroll
    for (int i = 0; i < 8; i++) v |= ((uint64_t)p[i]) << (8 * i);
    return v;
}

__device__ __forceinline__ void store64_be(uint8_t* p, uint64_t v) {
    #pragma unroll
    for (int i = 0; i < 8; i++) p[i] = (uint8_t)(v >> (56 - 8 * i));
}

__device__ bool hash_less_than(const uint8_t hash[32], const uint8_t difficulty[32]) {
    #pragma unroll
    for (int i = 0; i < 32; i++) {
        if (hash[i] < difficulty[i]) return true;
        if (hash[i] > difficulty[i]) return false;
    }
    return false;
}

__device__ void keccak256_64(const uint8_t msg[64], uint8_t out[32]) {
    uint8_t block[136];
    #pragma unroll
    for (int i = 0; i < 136; i++) block[i] = 0;
    #pragma unroll
    for (int i = 0; i < 64; i++) block[i] = msg[i];
    block[64] ^= 0x01;
    block[135] ^= 0x80;

    uint64_t st[25];
    #pragma unroll
    for (int i = 0; i < 25; i++) st[i] = 0;
    #pragma unroll
    for (int i = 0; i < 17; i++) st[i] ^= load64_le(block + i * 8);
    keccakf(st);

    #pragma unroll
    for (int i = 0; i < 4; i++) {
        uint64_t v = st[i];
        #pragma unroll
        for (int j = 0; j < 8; j++) out[i * 8 + j] = (uint8_t)(v >> (8 * j));
    }
}

__global__ void mine_kernel(
    const uint8_t* challenge,
    const uint8_t* difficulty,
    const uint8_t* prefix,
    uint64_t start,
    uint64_t total,
    int* found,
    uint64_t* found_counter,
    uint8_t* found_hash
) {
    uint64_t tid = (uint64_t)blockIdx.x * blockDim.x + threadIdx.x;
    uint64_t stride = (uint64_t)gridDim.x * blockDim.x;
    uint8_t msg[64];
    uint8_t hash[32];

    for (uint64_t i = tid; i < total && atomicAdd(found, 0) == 0; i += stride) {
        uint64_t counter = start + i;
        #pragma unroll
        for (int j = 0; j < 32; j++) msg[j] = challenge[j];
        #pragma unroll
        for (int j = 0; j < 24; j++) msg[32 + j] = prefix[j];
        store64_be(msg + 56, counter);

        keccak256_64(msg, hash);
        if (hash_less_than(hash, difficulty)) {
            if (atomicCAS(found, 0, 1) == 0) {
                *found_counter = counter;
                #pragma unroll
                for (int j = 0; j < 32; j++) found_hash[j] = hash[j];
            }
            return;
        }
    }
}

static int hexval(char c) {
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}

static std::vector<uint8_t> parse_hex(std::string s, size_t expect) {
    if (s.rfind("0x", 0) == 0 || s.rfind("0X", 0) == 0) s = s.substr(2);
    if (s.size() != expect * 2) {
        std::cerr << "bad hex length: expected " << expect * 2 << " got " << s.size() << "\n";
        std::exit(2);
    }
    std::vector<uint8_t> out(expect);
    for (size_t i = 0; i < expect; i++) {
        int hi = hexval(s[i * 2]);
        int lo = hexval(s[i * 2 + 1]);
        if (hi < 0 || lo < 0) {
            std::cerr << "bad hex\n";
            std::exit(2);
        }
        out[i] = (uint8_t)((hi << 4) | lo);
    }
    return out;
}

static std::string hex_bytes(const uint8_t* p, size_t n) {
    static const char* h = "0123456789abcdef";
    std::string s;
    s.reserve(n * 2);
    for (size_t i = 0; i < n; i++) {
        s.push_back(h[p[i] >> 4]);
        s.push_back(h[p[i] & 15]);
    }
    return s;
}

static void list_devices() {
    int count = 0;
    cudaError_t err = cudaGetDeviceCount(&count);
    if (err != cudaSuccess) {
        std::cerr << "cuda get device count failed: " << cudaGetErrorString(err) << "\n";
        std::exit(1);
    }
    std::cout << "{\"count\":" << count << ",\"devices\":[";
    for (int i = 0; i < count; i++) {
        cudaDeviceProp p{};
        cudaGetDeviceProperties(&p, i);
        if (i) std::cout << ",";
        long long score = (long long)p.multiProcessorCount * 1000 + p.major * 100 + p.minor;
        std::cout << "{\"id\":" << i
                  << ",\"name\":\"" << p.name
                  << "\",\"major\":" << p.major
                  << ",\"minor\":" << p.minor
                  << ",\"multiProcessorCount\":" << p.multiProcessorCount
                  << ",\"totalGlobalMem\":" << (unsigned long long)p.totalGlobalMem
                  << ",\"score\":" << score << "}";
    }
    std::cout << "]}" << std::endl;
}

int main(int argc, char** argv) {
    if (argc == 2 && std::string(argv[1]) == "--list") {
        list_devices();
        return 0;
    }
    if (argc < 8) {
        std::cerr << "usage: " << argv[0] << " challenge32 difficulty32 prefix24 batch blocks threads device\n";
        return 2;
    }

    auto challenge = parse_hex(argv[1], 32);
    auto difficulty = parse_hex(argv[2], 32);
    auto prefix = parse_hex(argv[3], 24);
    uint64_t batch = std::strtoull(argv[4], nullptr, 10);
    int blocks = std::atoi(argv[5]);
    int threads = std::atoi(argv[6]);
    int device = std::atoi(argv[7]);

    cudaSetDevice(device);
    uint8_t *d_challenge = nullptr, *d_difficulty = nullptr, *d_prefix = nullptr, *d_hash = nullptr;
    int* d_found = nullptr;
    uint64_t* d_counter = nullptr;
    cudaMalloc(&d_challenge, 32);
    cudaMalloc(&d_difficulty, 32);
    cudaMalloc(&d_prefix, 24);
    cudaMalloc(&d_found, sizeof(int));
    cudaMalloc(&d_counter, sizeof(uint64_t));
    cudaMalloc(&d_hash, 32);
    cudaMemcpy(d_challenge, challenge.data(), 32, cudaMemcpyHostToDevice);
    cudaMemcpy(d_difficulty, difficulty.data(), 32, cudaMemcpyHostToDevice);
    cudaMemcpy(d_prefix, prefix.data(), 24, cudaMemcpyHostToDevice);

    uint64_t start = 0;
    uint64_t total_done = 0;
    auto t0 = std::chrono::steady_clock::now();
    auto last = t0;
    uint64_t last_total = 0;

    while (true) {
        int zero = 0;
        cudaMemcpy(d_found, &zero, sizeof(int), cudaMemcpyHostToDevice);
        mine_kernel<<<blocks, threads>>>(d_challenge, d_difficulty, d_prefix, start, batch, d_found, d_counter, d_hash);
        cudaDeviceSynchronize();
        cudaError_t err = cudaGetLastError();
        if (err != cudaSuccess) {
            std::cerr << "cuda error: " << cudaGetErrorString(err) << "\n";
            return 1;
        }

        int found = 0;
        cudaMemcpy(&found, d_found, sizeof(int), cudaMemcpyDeviceToHost);
        if (found) {
            uint64_t ctr = 0;
            uint8_t h[32];
            cudaMemcpy(&ctr, d_counter, sizeof(uint64_t), cudaMemcpyDeviceToHost);
            cudaMemcpy(h, d_hash, 32, cudaMemcpyDeviceToHost);
            uint8_t nonce[32] = {0};
            std::memcpy(nonce, prefix.data(), 24);
            for (int i = 0; i < 8; i++) nonce[24 + i] = (uint8_t)(ctr >> (56 - 8 * i));
            std::cout << "{\"type\":\"found\",\"nonce\":\"0x" << hex_bytes(nonce, 32)
                      << "\",\"result\":\"0x" << hex_bytes(h, 32) << "\"}" << std::endl;
            return 0;
        }

        start += batch;
        total_done += batch;
        auto now = std::chrono::steady_clock::now();
        double dt = std::chrono::duration<double>(now - last).count();
        if (dt >= 1.0) {
            double hps = (double)(total_done - last_total) / dt;
            std::cout << "{\"type\":\"progress\",\"total\":\"" << total_done
                      << "\",\"hps\":" << hps << "}" << std::endl;
            last = now;
            last_total = total_done;
        }
    }
}
