/*
 * brn_merkle.c — Merkle root em C para BRN
 * ================================================================
 * v2 (2026-11-03): FIX CRÍTICO
 *
 *   [PROBLEMA ANTERIOR]
 *     A versão v1 combinava os pares com double_sha256 (SHA256 duas
 *     vezes), seguindo Bitcoin. Mas o blockchain.py do BRN usa
 *     SHA256 SIMPLES:
 *
 *         layer = [sha256(layer[i] + layer[i+1]) for i in ...]
 *
 *     Ou seja, apenas UMA passada de SHA256. Resultado: o hash do
 *     merkle gerado pelo C era DIFERENTE do Python. Se o C fosse
 *     usado para validar blocos, todo merkle divergiria = fork
 *     imediato.
 *
 *   [CORREÇÃO v2]
 *     - combina pares com SHA256 SIMPLES (1 passada)
 *     - caso especial 1 txid: retorna o txid cru (sem hash),
 *       porque blockchain.compute_merkle_root() faz
 *           layer = [bytes.fromhex(t) for t in txids]
 *           # len(layer) == 1 → loop não executa
 *           return layer[0].hex()
 *       que é o próprio txid sem transformação.
 *
 * ================================================================
 * Compilar Linux:
 *   gcc -O3 -march=native -shared -fPIC -o brn_merkle.so brn_merkle.c -lcrypto
 *
 * Compilar Windows (MSYS2/MinGW):
 *   gcc -O3 -shared -o brn_merkle.dll brn_merkle.c -lcrypto -lssl
 *
 * Requer: OpenSSL (libcrypto)
 *   Linux  : sudo apt install libssl-dev
 *   Windows: pacman -S mingw-w64-x86_64-openssl
 * ================================================================
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <openssl/sha.h>
#include <stdint.h>

/*
 * SHA256 SIMPLES (1 passada) — é o que blockchain.py usa no merkle.
 * Alias semântico de SHA256() para deixar a intenção clara.
 */
static void sha256_once(const unsigned char *data, size_t len,
                        unsigned char *out) {
    SHA256(data, len, out);
}

/*
 * SHA256 DUPLO (2 passadas) — NÃO usado no merkle.
 * Mantido só por compat (outros módulos podem usar).
 */
void double_sha256(const unsigned char *data, size_t len,
                   unsigned char *out) {
    unsigned char hash1[SHA256_DIGEST_LENGTH];
    SHA256(data, len, hash1);
    SHA256(hash1, SHA256_DIGEST_LENGTH, out);
}

/*
 * compute_merkle_root_c — merkle root compatível com Python
 *
 * Entrada:
 *   txids_bytes : buffer com N txids concatenados (cada um 32 bytes)
 *   num_txids   : número de txids
 *   out_hash    : buffer de 32 bytes para o resultado
 *
 * Comportamento (idêntico ao Python):
 *   - num_txids == 0 → out_hash = 0x00...00 (32 bytes)
 *   - num_txids == 1 → out_hash = txids_bytes[0..32] (cru, SEM hash)
 *   - num_txids >= 2 → SHA256 SIMPLES por par, duplica o último se ímpar
 */
void compute_merkle_root_c(const unsigned char *txids_bytes,
                           int num_txids,
                           unsigned char *out_hash) {
    /* Caso 0: sem txids */
    if (num_txids <= 0) {
        memset(out_hash, 0, 32);
        return;
    }

    /* Caso 1: único txid — retorna como está (sem hash).
       Isso bate com blockchain.compute_merkle_root:
           layer = [bytes.fromhex(t) for t in txids]
           # len(layer) == 1 → while não executa
           return layer[0].hex()  ← é o txid puro
    */
    if (num_txids == 1) {
        memcpy(out_hash, txids_bytes, 32);
        return;
    }

    /* Caso 2+: construir a árvore */
    int current_count = num_txids;
    size_t layer_size = (size_t)current_count * 32;
    unsigned char *layer = (unsigned char *)malloc(layer_size);
    if (!layer) {
        /* Sem memória: retorna zeros para não crashar.
           O chamador deve detectar (hash inválido → rejeita bloco). */
        memset(out_hash, 0, 32);
        return;
    }
    memcpy(layer, txids_bytes, layer_size);

    while (current_count > 1) {
        int next_count = (current_count + 1) / 2;
        size_t next_size = (size_t)next_count * 32;
        unsigned char *next_layer = (unsigned char *)malloc(next_size);
        if (!next_layer) {
            free(layer);
            memset(out_hash, 0, 32);
            return;
        }

        for (int i = 0; i < current_count; i += 2) {
            unsigned char combined[64];
            memcpy(combined, layer + (i * 32), 32);

            if (i + 1 < current_count) {
                memcpy(combined + 32, layer + ((i + 1) * 32), 32);
            } else {
                /* Número ímpar: duplica o último, igual ao Python
                   (if len(layer) % 2: layer.append(layer[-1])) */
                memcpy(combined + 32, layer + (i * 32), 32);
            }

            /* SHA256 SIMPLES — CORREÇÃO PRINCIPAL.
               NÃO usar double_sha256 aqui. */
            sha256_once(combined, 64, next_layer + ((i / 2) * 32));
        }

        free(layer);
        layer = next_layer;
        current_count = next_count;
    }

    memcpy(out_hash, layer, 32);
    free(layer);
}