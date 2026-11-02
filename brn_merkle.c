#include <openssl/sha.h>
#include <string.h>

// Recebe um array de bytes das txids e retorna o hash final
void compute_merkle_root_c(const unsigned char* txids_data, int num_txids, unsigned char* out_hash) {
    // Lógica de pareamento e SHA256 duplo em C
}
