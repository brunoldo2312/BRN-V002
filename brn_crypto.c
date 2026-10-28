#include <stdio.h>
#include <string.h>
#include <stdint.h>
#include <secp256k1.h>
#include <secp256k1_extrakeys.h>
#include <secp256k1_schnorrsig.h>

#ifdef _WIN32
  #define EXPORT __declspec(dllexport)
#else
  #define EXPORT __attribute__((visibility("default")))
#endif

// Contextos globais (Verificação é thread-safe por padrão no secp256k1)
static secp256k1_context* ctx = NULL;

static void init_ctx() {
    if (ctx == NULL) {
        ctx = secp256k1_context_create(SECP256K1_CONTEXT_VERIFY | SECP256K1_CONTEXT_SIGN);
        // Define uma seed fixa para o contexto, suficiente para verificação.
        unsigned char seed[32] = {0};
        secp256k1_context_randomize(ctx, seed);
    }
}

// ============================================================
// ECDSA
// ============================================================
EXPORT int brn_verify_ecdsa(const unsigned char* pubkey, int pubkey_len, 
                            const unsigned char* sig64, const unsigned char* msg32) {
    init_ctx();
    secp256k1_pubkey pub;
    secp256k1_ecdsa_signature sig;

    if (!secp256k1_ec_pubkey_parse(ctx, &pub, pubkey, pubkey_len)) return 0;
    if (!secp256k1_ecdsa_signature_parse_compact(ctx, &sig, sig64)) return 0;
    
    // A libsecp256k1 já rejeita assinaturas "High-S", o que é perfeito.
    return secp256k1_ecdsa_verify(ctx, &sig, msg32, &pub);
}

EXPORT int brn_sign_ecdsa(const unsigned char* privkey, const unsigned char* msg32, 
                          unsigned char* out_sig64) {
    init_ctx();
    secp256k1_ecdsa_signature sig;
    
    // Assinatura determinística (RFC6979) - muito mais segura que a atual no Python puro
    if (!secp256k1_ecdsa_sign(ctx, &sig, msg32, privkey, NULL, NULL)) return 0;
    
    secp256k1_ecdsa_signature_serialize_compact(ctx, out_sig64, &sig);
    return 1;
}

// ============================================================
// SCHNORR BIP-340
// ============================================================
EXPORT int brn_verify_schnorr(const unsigned char* pubkey, int pubkey_len, 
                              const unsigned char* sig64, const unsigned char* msg32) {
    init_ctx();
    secp256k1_xonly_pubkey pubkey_x;
    secp256k1_keypair keypair; (void)keypair;

    unsigned char pub_x[32];
    
    // Se o Python enviar pubkey de 33 bytes (comprimida 02/03), extraímos o X.
    // O BIP-340 exige pubkeys de 32 bytes (X-only).
    if (pubkey_len == 33) {
        memcpy(pub_x, pubkey + 1, 32);
    } else if (pubkey_len == 32) {
        memcpy(pub_x, pubkey, 32);
    } else {
        return 0;
    }

    if (!secp256k1_xonly_pubkey_parse(ctx, &pubkey_x, pub_x)) return 0;
    
    return secp256k1_schnorrsig_verify(ctx, sig64, msg32, 32, &pubkey_x);
}

EXPORT int brn_sign_schnorr(const unsigned char* privkey, const unsigned char* msg32, 
                            const unsigned char* aux_rand32, unsigned char* out_sig64) {
    init_ctx();
    secp256k1_keypair keypair;
    
    if (!secp256k1_keypair_create(ctx, &keypair, privkey)) return 0;
    
    return secp256k1_schnorrsig_sign32(ctx, out_sig64, msg32, &keypair, aux_rand32);
}