// Bare-metal Ampere sm_86 silicon roofline & Portal 53 (A_16 / Block-H2 / WHT_8) probe
// Target: ASUS ZenBook Pro Duo UX582HS (NVIDIA GeForce RTX 3080 Laptop GPU, GA104M, CC 8.6)
// Build:  /opt/cuda/bin/nvcc -O3 -std=c++17 -arch=sm_86 rtx3080_sm86_silicon_probe.cu -o rtx3080_sm86_silicon_probe

#include <cuda_runtime.h>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <vector>

#define CUDA_CHECK(call) do {                                                 \
    cudaError_t _e = (call);                                                  \
    if (_e != cudaSuccess) {                                                  \
        fprintf(stderr, "CUDA_ERROR %s:%d: %s\n", __FILE__, __LINE__,         \
                cudaGetErrorString(_e));                                      \
        exit(1);                                                              \
    }                                                                         \
} while (0)

// 1. Sustained GDDR6 STREAM Triad (float4 vectorized)
__global__ void stream_triad_kernel(float4* __restrict__ a,
                                    const float4* __restrict__ b,
                                    const float4* __restrict__ c,
                                    float s, size_t n4) {
    size_t idx = (size_t)blockIdx.x * blockDim.x + threadIdx.x;
    size_t stride = (size_t)gridDim.x * blockDim.x;
    for (size_t i = idx; i < n4; i += stride) {
        float4 bv = b[i];
        float4 cv = c[i];
        float4 av;
        av.x = bv.x + s * cv.x;
        av.y = bv.y + s * cv.y;
        av.z = bv.z + s * cv.z;
        av.w = bv.w + s * cv.w;
        a[i] = av;
    }
}

// 2. Register-bound FP32 FMA peak kernel (8 independent FMA chains -> hides 4-cycle latency)
__global__ void fp32_fma_peak_kernel(float* __restrict__ out, int iters) {
    float a0 = 1.0001f + (threadIdx.x & 15) * 1e-4f;
    float a1 = 1.0002f, a2 = 1.0003f, a3 = 1.0004f;
    float a4 = 1.0005f, a5 = 1.0006f, a6 = 1.0007f, a7 = 1.0008f;
    const float m = 0.999999f;
    const float b = 0.000001f;
    #pragma unroll 4
    for (int i = 0; i < iters; ++i) {
        a0 = fmaf(a0, m, b); a1 = fmaf(a1, m, b);
        a2 = fmaf(a2, m, b); a3 = fmaf(a3, m, b);
        a4 = fmaf(a4, m, b); a5 = fmaf(a5, m, b);
        a6 = fmaf(a6, m, b); a7 = fmaf(a7, m, b);
    }
    if (blockIdx.x == 0 && threadIdx.x == 0) {
        out[0] = a0 + a1 + a2 + a3 + a4 + a5 + a6 + a7;
    }
}

// 3. Register-bound INT8 dp4a (__dp4a: 4 int8 MACs = 8 ops per instruction) throughput kernel
__global__ void int8_dp4a_peak_kernel(int* __restrict__ out, int iters) {
    int p0 = 0x01020304 + (threadIdx.x & 7);
    int p1 = 0x05060708;
    int c0 = 0, c1 = 1, c2 = 2, c3 = 3;
    int c4 = 4, c5 = 5, c6 = 6, c7 = 7;
    #pragma unroll 4
    for (int i = 0; i < iters; ++i) {
        c0 = __dp4a(p0, p1, c0); c1 = __dp4a(p0, p1, c1);
        c2 = __dp4a(p0, p1, c2); c3 = __dp4a(p0, p1, c3);
        c4 = __dp4a(p0, p1, c4); c5 = __dp4a(p0, p1, c5);
        c6 = __dp4a(p0, p1, c6); c7 = __dp4a(p0, p1, c7);
    }
    if (blockIdx.x == 0 && threadIdx.x == 0) {
        out[0] = c0 + c1 + c2 + c3 + c4 + c5 + c6 + c7;
    }
}

// 4A. Portal 53 / Track S47: Branchless Extended Hamming [16, 11, 4] Construction-A A_16
//     1-flip syndrome-table decoder on 16-D quantized int8 input blocks.
//     Parity-check rows for standard [16,11,4] extended Hamming (positions 0..15):
//       H0 (overall parity): 0xFFFF
//       H1 (bit 0 of pos):   0xAAAA  (positions 1,3,5,7,9,11,13,15)
//       H2 (bit 1 of pos):   0xCCCC  (positions 2,3,6,7,10,11,14,15)
//       H3 (bit 2 of pos):   0xF0F0  (positions 4,5,6,7,12,13,14,15)
//       H4 (bit 3 of pos):   0xFF00  (positions 8..15)
//     If overall parity == 1: syndrome s = (p4<<3)|(p3<<2)|(p2<<1)|p1 gives exact 1-flip coordinate!
//     (For even-parity nonzero syndrome -> weight-2 tie; break to closest uncertain coord.)
__global__ void hamming_a16_syndrome_decode_kernel(const int8_t* __restrict__ in_blocks,
                                                   int8_t* __restrict__ out_blocks,
                                                   uint32_t* __restrict__ err_count,
                                                   int n_blocks) {
    int b = blockIdx.x * blockDim.x + threadIdx.x;
    if (b >= n_blocks) return;

    // Vectorized 16-byte load (one int4 per block = 16 int8 coordinates)
    int4 raw = reinterpret_cast<const int4*>(in_blocks)[b];
    int8_t v[16];
    reinterpret_cast<int4*>(v)[0] = raw;

    // Extract initial parity bits (LSB of rounded int8 coords) + locate weakest margin
    uint32_t bits = 0;
    int min_abs = 256;
    int min_idx = 0;
    #pragma unroll
    for (int i = 0; i < 16; ++i) {
        int val = (int)v[i];
        bits |= ((uint32_t)(val & 1)) << i;
        int av = val < 0 ? -val : val;
        if (av < min_abs) { min_abs = av; min_idx = i; }
    }

    // 5-bit extended Hamming syndrome via hardware __popc (single-cycle on sm_86)
    uint32_t p0 = __popc(bits) & 1u;
    uint32_t s1 = __popc(bits & 0xAAAAu) & 1u;
    uint32_t s2 = __popc(bits & 0xCCCCu) & 1u;
    uint32_t s3 = __popc(bits & 0xF0F0u) & 1u;
    uint32_t s4 = __popc(bits & 0xFF00u) & 1u;
    uint32_t syn = s1 | (s2 << 1) | (s3 << 2) | (s4 << 3);

    // Branchless single-flip correction for odd parity; 2-flip fallback for even nonzero syndrome
    if (p0) {
        // Unique coset leader flips exact coordinate `syn`
        bits ^= (1u << syn);
        v[syn] = (v[syn] >= 0) ? (int8_t)(v[syn] - 1) : (int8_t)(v[syn] + 1);
    } else if (syn != 0u) {
        // Weight-2 coset: flip weakest coordinate `min_idx` and its partner `min_idx ^ syn`
        int partner = min_idx ^ (int)syn;
        bits ^= (1u << min_idx) | (1u << partner);
        v[min_idx] = (v[min_idx] >= 0) ? (int8_t)(v[min_idx] - 1) : (int8_t)(v[min_idx] + 1);
        v[partner] = (v[partner] >= 0) ? (int8_t)(v[partner] - 1) : (int8_t)(v[partner] + 1);
    }

    // Exact hardware verification: output parity bits MUST satisfy all 5 Hamming checks!
    uint32_t chk = (__popc(bits) & 1u)
                 | ((__popc(bits & 0xAAAAu) & 1u) << 1)
                 | ((__popc(bits & 0xCCCCu) & 1u) << 2)
                 | ((__popc(bits & 0xF0F0u) & 1u) << 3)
                 | ((__popc(bits & 0xFF00u) & 1u) << 4);
    if (chk != 0u) atomicAdd(err_count, 1u);

    reinterpret_cast<int4*>(out_blocks)[b] = reinterpret_cast<const int4*>(v)[0];
}

// 4B. Portal 53 / Track S32: Block-H2 INT8-safe transform (exact pairs (a+b, a-b), peak 78<=127)
__global__ void block_h2_int8_kernel(const int8_t* __restrict__ in_blocks,
                                     int8_t* __restrict__ out_blocks,
                                     int n_blocks) {
    int b = blockIdx.x * blockDim.x + threadIdx.x;
    if (b >= n_blocks) return;
    int4 raw = reinterpret_cast<const int4*>(in_blocks)[b];
    int8_t v[16];
    reinterpret_cast<int4*>(v)[0] = raw;
    #pragma unroll
    for (int i = 0; i < 16; i += 2) {
        int a = (int)v[i];
        int c = (int)v[i + 1];
        v[i]     = (int8_t)((a + c) >> 1);
        v[i + 1] = (int8_t)((a - c) >> 1);
    }
    reinterpret_cast<int4*>(out_blocks)[b] = reinterpret_cast<const int4*>(v)[0];
}

// 4C. Portal 53 / Track S32: Full WHT_8 (two 8-D butterfly blocks per 16-D block)
__global__ void wht8_int32_kernel(const int8_t* __restrict__ in_blocks,
                                  int8_t* __restrict__ out_blocks,
                                  int n_blocks) {
    int b = blockIdx.x * blockDim.x + threadIdx.x;
    if (b >= n_blocks) return;
    int4 raw = reinterpret_cast<const int4*>(in_blocks)[b];
    int8_t v[16];
    reinterpret_cast<int4*>(v)[0] = raw;
    #pragma unroll
    for (int half = 0; half < 16; half += 8) {
        int x[8];
        #pragma unroll
        for (int i = 0; i < 8; ++i) x[i] = (int)v[half + i];
        // Stage 1 (stride 1)
        #pragma unroll
        for (int i = 0; i < 8; i += 2) { int a = x[i], c = x[i+1]; x[i] = a+c; x[i+1] = a-c; }
        // Stage 2 (stride 2)
        #pragma unroll
        for (int i = 0; i < 8; i += 4) {
            int a0 = x[i], c0 = x[i+2], a1 = x[i+1], c1 = x[i+3];
            x[i] = a0+c0; x[i+2] = a0-c0; x[i+1] = a1+c1; x[i+3] = a1-c1;
        }
        // Stage 3 (stride 4)
        #pragma unroll
        for (int i = 0; i < 4; ++i) {
            int a = x[i], c = x[i+4];
            v[half + i]     = (int8_t)((a + c) >> 3);
            v[half + i + 4] = (int8_t)((a - c) >> 3);
        }
    }
    reinterpret_cast<int4*>(out_blocks)[b] = reinterpret_cast<const int4*>(v)[0];
}

int main() {
    cudaDeviceProp prop{};
    CUDA_CHECK(cudaGetDeviceProperties(&prop, 0));
    int memClockKHz = 0, coreClockKHz = 0;
    CUDA_CHECK(cudaDeviceGetAttribute(&memClockKHz, cudaDevAttrMemoryClockRate, 0));
    CUDA_CHECK(cudaDeviceGetAttribute(&coreClockKHz, cudaDevAttrClockRate, 0));
    double peak_bw_gbs = 2.0 * (double)memClockKHz * 1e3 * (prop.memoryBusWidth / 8.0) / 1e9;

    printf("=== RTX 3080 AMPERE SILICON SPEC (cudaGetDeviceProperties) ===\n");
    printf("  Device:           %s (CC %d.%d)\n", prop.name, prop.major, prop.minor);
    printf("  SMs:              %d SMs (%d threads/SM, max %d resident warps/SM)\n",
           prop.multiProcessorCount, prop.maxThreadsPerMultiProcessor,
           prop.maxThreadsPerMultiProcessor / prop.warpSize);
    printf("  Shared Mem / SM:  %zu KiB | L2 Cache: %.2f MiB | VRAM: %.0f MiB\n",
           prop.sharedMemPerMultiprocessor / 1024, prop.l2CacheSize / (1024.0 * 1024.0),
           prop.totalGlobalMem / (1024.0 * 1024.0));
    printf("  Bus / Peak BW:    %d-bit GDDR6 @ %.0f MHz -> %.1f GB/s theoretical\n",
           prop.memoryBusWidth, memClockKHz / 1000.0, peak_bw_gbs);
    printf("  Boost Clock:      %.0f MHz\n", coreClockKHz / 1000.0);

    cudaEvent_t t0, t1;
    CUDA_CHECK(cudaEventCreate(&t0));
    CUDA_CHECK(cudaEventCreate(&t1));
    float ms = 0.0f;

    // 1. STREAM Triad (64M floats = 256 MiB per array -> 768 MiB total, >> 4 MiB L2)
    const size_t N4 = (64ULL * 1024 * 1024) / 4;
    const size_t bytes_arr = N4 * sizeof(float4);
    float4 *da = nullptr, *db = nullptr, *dc = nullptr;
    CUDA_CHECK(cudaMalloc(&da, bytes_arr));
    CUDA_CHECK(cudaMalloc(&db, bytes_arr));
    CUDA_CHECK(cudaMalloc(&dc, bytes_arr));
    CUDA_CHECK(cudaMemset(db, 1, bytes_arr));
    CUDA_CHECK(cudaMemset(dc, 2, bytes_arr));
    int triad_blocks = prop.multiProcessorCount * 32;
    stream_triad_kernel<<<triad_blocks, 256>>>(da, db, dc, 1.5f, N4);
    CUDA_CHECK(cudaDeviceSynchronize());
    const int triad_iters = 20;
    CUDA_CHECK(cudaEventRecord(t0));
    for (int i = 0; i < triad_iters; ++i)
        stream_triad_kernel<<<triad_blocks, 256>>>(da, db, dc, 1.5f, N4);
    CUDA_CHECK(cudaEventRecord(t1));
    CUDA_CHECK(cudaEventSynchronize(t1));
    CUDA_CHECK(cudaEventElapsedTime(&ms, t0, t1));
    double triad_gbs = (3.0 * (double)bytes_arr * triad_iters) / (ms * 1e6);
    printf("\n=== EMPIRICAL SILICON ROOF-LINES ===\n");
    printf("  GDDR6 STREAM Triad (256 MiB x3): %.2f GB/s (%.1f%% of %.1f GB/s peak)\n",
           triad_gbs, 100.0 * triad_gbs / peak_bw_gbs, peak_bw_gbs);
    CUDA_CHECK(cudaFree(da)); CUDA_CHECK(cudaFree(db)); CUDA_CHECK(cudaFree(dc));

    // 2. PCIe Gen4 x8 Pinned H2D & D2H (128 MiB)
    const size_t pcie_bytes = 128ULL * 1024 * 1024;
    void *h_pin = nullptr, *d_buf = nullptr;
    CUDA_CHECK(cudaMallocHost(&h_pin, pcie_bytes));
    CUDA_CHECK(cudaMalloc(&d_buf, pcie_bytes));
    memset(h_pin, 0x5A, pcie_bytes);
    CUDA_CHECK(cudaMemcpy(d_buf, h_pin, pcie_bytes, cudaMemcpyHostToDevice));
    const int pcie_iters = 10;
    CUDA_CHECK(cudaEventRecord(t0));
    for (int i = 0; i < pcie_iters; ++i)
        CUDA_CHECK(cudaMemcpy(d_buf, h_pin, pcie_bytes, cudaMemcpyHostToDevice));
    CUDA_CHECK(cudaEventRecord(t1));
    CUDA_CHECK(cudaEventSynchronize(t1));
    CUDA_CHECK(cudaEventElapsedTime(&ms, t0, t1));
    double h2d_gbs = ((double)pcie_bytes * pcie_iters) / (ms * 1e6);
    CUDA_CHECK(cudaEventRecord(t0));
    for (int i = 0; i < pcie_iters; ++i)
        CUDA_CHECK(cudaMemcpy(h_pin, d_buf, pcie_bytes, cudaMemcpyDeviceToHost));
    CUDA_CHECK(cudaEventRecord(t1));
    CUDA_CHECK(cudaEventSynchronize(t1));
    CUDA_CHECK(cudaEventElapsedTime(&ms, t0, t1));
    double d2h_gbs = ((double)pcie_bytes * pcie_iters) / (ms * 1e6);
    printf("  PCIe Gen4 x8 Pinned Transfer:    H2D %.2f GB/s | D2H %.2f GB/s\n", h2d_gbs, d2h_gbs);
    CUDA_CHECK(cudaFreeHost(h_pin)); CUDA_CHECK(cudaFree(d_buf));

    // 3. FP32 FMA & INT8 dp4a Throughput
    float* d_fout = nullptr; int* d_iout = nullptr;
    CUDA_CHECK(cudaMalloc(&d_fout, sizeof(float)));
    CUDA_CHECK(cudaMalloc(&d_iout, sizeof(int)));
    int cblocks = prop.multiProcessorCount * 8;
    int cthreads = 256;
    int citers = 8192;
    fp32_fma_peak_kernel<<<cblocks, cthreads>>>(d_fout, 64);
    CUDA_CHECK(cudaDeviceSynchronize());
    CUDA_CHECK(cudaEventRecord(t0));
    fp32_fma_peak_kernel<<<cblocks, cthreads>>>(d_fout, citers);
    CUDA_CHECK(cudaEventRecord(t1));
    CUDA_CHECK(cudaEventSynchronize(t1));
    CUDA_CHECK(cudaEventElapsedTime(&ms, t0, t1));
    // 8 FMAs/iter = 16 FLOPs/iter/thread
    double fp32_tflops = ((double)cblocks * cthreads * citers * 16.0) / (ms * 1e9);

    int8_dp4a_peak_kernel<<<cblocks, cthreads>>>(d_iout, 64);
    CUDA_CHECK(cudaDeviceSynchronize());
    CUDA_CHECK(cudaEventRecord(t0));
    int8_dp4a_peak_kernel<<<cblocks, cthreads>>>(d_iout, citers);
    CUDA_CHECK(cudaEventRecord(t1));
    CUDA_CHECK(cudaEventSynchronize(t1));
    CUDA_CHECK(cudaEventElapsedTime(&ms, t0, t1));
    // 8 dp4a/iter * 8 INT8 ops/dp4a = 64 INT8 ops/iter/thread
    double dp4a_tops = ((double)cblocks * cthreads * citers * 64.0) / (ms * 1e9);
    printf("  FP32 FMA Sustained Compute:      %.2f TFLOPS\n", fp32_tflops);
    printf("  INT8 dp4a Sustained Compute:     %.2f TOPS\n", dp4a_tops);
    CUDA_CHECK(cudaFree(d_fout)); CUDA_CHECK(cudaFree(d_iout));

    // 4. First-Light Portal 53 / S47 / S32 Kernel Comparison (4,194,304 16-D blocks = 67.1M weights = 64 MiB)
    const int N_BLOCKS = 4 * 1024 * 1024;
    const size_t qbytes = (size_t)N_BLOCKS * 16;
    std::vector<int8_t> h_in(qbytes);
    uint32_t rng = 0xA1E7E1Au;
    for (size_t i = 0; i < qbytes; ++i) {
        rng = rng * 1664525u + 1013904223u;
        h_in[i] = (int8_t)(((int)(rng >> 24) % 79) - 39); // [-39, +39] -> Block-H2 max 78 <= 127!
    }
    int8_t *d_qin = nullptr, *d_qout = nullptr;
    uint32_t *d_err = nullptr, h_err = 0;
    CUDA_CHECK(cudaMalloc(&d_qin, qbytes));
    CUDA_CHECK(cudaMalloc(&d_qout, qbytes));
    CUDA_CHECK(cudaMalloc(&d_err, sizeof(uint32_t)));
    CUDA_CHECK(cudaMemcpy(d_qin, h_in.data(), qbytes, cudaMemcpyHostToDevice));
    CUDA_CHECK(cudaMemset(d_err, 0, sizeof(uint32_t)));

    int qgrid = (N_BLOCKS + 255) / 256;
    const int qiters = 50;

    // 4A. A_16 Hamming [16,11,4] 1-flip syndrome decode
    hamming_a16_syndrome_decode_kernel<<<qgrid, 256>>>(d_qin, d_qout, d_err, N_BLOCKS);
    CUDA_CHECK(cudaDeviceSynchronize());
    CUDA_CHECK(cudaMemcpy(&h_err, d_err, sizeof(uint32_t), cudaMemcpyDeviceToHost));
    CUDA_CHECK(cudaMemset(d_err, 0, sizeof(uint32_t)));
    CUDA_CHECK(cudaEventRecord(t0));
    for (int i = 0; i < qiters; ++i)
        hamming_a16_syndrome_decode_kernel<<<qgrid, 256>>>(d_qin, d_qout, d_err, N_BLOCKS);
    CUDA_CHECK(cudaEventRecord(t1));
    CUDA_CHECK(cudaEventSynchronize(t1));
    CUDA_CHECK(cudaEventElapsedTime(&ms, t0, t1));
    CUDA_CHECK(cudaMemcpy(&h_err, d_err, sizeof(uint32_t), cudaMemcpyDeviceToHost));
    double a16_ms = ms / qiters;
    double a16_gw = ((double)N_BLOCKS * 16.0) / (a16_ms * 1e6);
    double a16_gbs = (2.0 * (double)qbytes) / (a16_ms * 1e6);

    // 4B. Block-H2 INT8
    block_h2_int8_kernel<<<qgrid, 256>>>(d_qin, d_qout, N_BLOCKS);
    CUDA_CHECK(cudaDeviceSynchronize());
    CUDA_CHECK(cudaEventRecord(t0));
    for (int i = 0; i < qiters; ++i)
        block_h2_int8_kernel<<<qgrid, 256>>>(d_qin, d_qout, N_BLOCKS);
    CUDA_CHECK(cudaEventRecord(t1));
    CUDA_CHECK(cudaEventSynchronize(t1));
    CUDA_CHECK(cudaEventElapsedTime(&ms, t0, t1));
    double h2_ms = ms / qiters;
    double h2_gw = ((double)N_BLOCKS * 16.0) / (h2_ms * 1e6);
    double h2_gbs = (2.0 * (double)qbytes) / (h2_ms * 1e6);

    // 4C. WHT_8 (2x8-D per 16-D block)
    wht8_int32_kernel<<<qgrid, 256>>>(d_qin, d_qout, N_BLOCKS);
    CUDA_CHECK(cudaDeviceSynchronize());
    CUDA_CHECK(cudaEventRecord(t0));
    for (int i = 0; i < qiters; ++i)
        wht8_int32_kernel<<<qgrid, 256>>>(d_qin, d_qout, N_BLOCKS);
    CUDA_CHECK(cudaEventRecord(t1));
    CUDA_CHECK(cudaEventSynchronize(t1));
    CUDA_CHECK(cudaEventElapsedTime(&ms, t0, t1));
    double wht_ms = ms / qiters;
    double wht_gw = ((double)N_BLOCKS * 16.0) / (wht_ms * 1e6);
    double wht_gbs = (2.0 * (double)qbytes) / (wht_ms * 1e6);

    printf("\n=== FIRST-LIGHT PORTAL 53 / S47 / S32 KERNELS (67.1M weights / 4.19M 16-D blocks) ===\n");
    printf("  A_16 [16,11,4] Syndrome Decode:  %.4f ms | %.2f Gweights/s | %.2f GB/s | syndrome_errs=%u\n",
           a16_ms, a16_gw, a16_gbs, h_err);
    printf("  Block-H2 INT8 (S32, max<=127):   %.4f ms | %.2f Gweights/s | %.2f GB/s\n",
           h2_ms, h2_gw, h2_gbs);
    printf("  WHT_8 Butterfly (2x8-D):         %.4f ms | %.2f Gweights/s | %.2f GB/s\n",
           wht_ms, wht_gw, wht_gbs);
    printf("  Ratio (A_16 / Block-H2 time):    %.3fx (memory-bandwidth bound!)\n", a16_ms / h2_ms);

    CUDA_CHECK(cudaFree(d_qin)); CUDA_CHECK(cudaFree(d_qout)); CUDA_CHECK(cudaFree(d_err));
    CUDA_CHECK(cudaEventDestroy(t0)); CUDA_CHECK(cudaEventDestroy(t1));
    return (h_err == 0) ? 0 : 2;
}
