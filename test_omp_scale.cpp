#include <iostream>
#include <vector>
#include <chrono>
#include <random>
#include <omp.h>
#include "hnsw_index.h"

int main() {
    size_t num_vectors = 100000;
    size_t dim = 768;

    std::vector<float> data(num_vectors * dim);
    std::vector<size_t> ids(num_vectors);
    std::mt19937 gen(42);
    std::uniform_real_distribution<float> dist(0.0, 1.0);

    for (size_t i = 0; i < num_vectors * dim; ++i) {
        data[i] = dist(gen);
    }
    for (size_t i = 0; i < num_vectors; ++i) {
        ids[i] = i;
    }

    HnswIndex index(dim, num_vectors);

    auto start = std::chrono::high_resolution_clock::now();

    index.BulkInsert(data.data(), ids, num_vectors);

    auto end = std::chrono::high_resolution_clock::now();
    auto duration = std::chrono::duration_cast<std::chrono::milliseconds>(end - start).count();

    std::cout << "Threads: " << omp_get_max_threads() << std::endl;
    std::cout << "Time: " << duration << " ms" << std::endl;

    return 0;
}
