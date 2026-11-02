#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <openssl/sha.h>
#include <stdint.h>

// SHA256 duplo
void double_sha256(const unsigned char* data, size_t len, unsigned char* out) {
    unsigned char hash1[SHA256_DIGEST_LENGTH];
    SHA256(data, len, hash1);
    SHA256(hash1, SHA256_DIGEST_LENGTH, out);
}

void compute_merkle_root_c(const unsigned char* txids_bytes, int num_txids, unsigned char* out_hash) {
    if (num_txids == 0) {
        memset(out_hash, 0, 32);
        return;
    }
    if (num_txids == 1) {
        double_sha256(txids_bytes, 32, out_hash);
        return;
    }

    int current_count = num_txids;
    int layer_size = current_count * 32;
    unsigned char* layer = (unsigned char*)malloc(layer_size);
    if (!layer) return;
    memcpy(layer, txids_bytes, layer_size);

    while (current_count > 1) {
        int next_count = (current_count + 1) / 2;
        int next_size = next_count * 32;
        unsigned char* next_layer = (unsigned char*)malloc(next_size);
        if (!next_layer) { free(layer); return; }

        for (int i = 0; i < current_count; i += 2) {
            unsigned char combined[64];
            memcpy(combined, layer + (i * 32), 32);
            if (i + 1 < current_count) {
                memcpy(combined + 32, layer + ((i + 1) * 32), 32);
            } else {
                memcpy(combined + 32, layer + (i * 32), 32); // Duplica o último
            }
            double_sha256(combined, 64, next_layer + ((i / 2) * 32));
        }

        free(layer);
        layer = next_layer;
        current_count = next_count;
    }

    memcpy(out_hash, layer, 32);
    free(layer);
}