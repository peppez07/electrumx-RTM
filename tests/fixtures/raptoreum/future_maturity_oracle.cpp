// Copyright (c) 2017-2023 The Bitcoin Core developers
// Copyright (c) 2020-2023 The Raptoreum developers
// Distributed under the MIT software license.
// Decision extracted from Core 900794ea94ff11023667765173295b459d32732c
// src/consensus/tx_verify.cpp validateFutureCoin. No node or network is used.
#include <cstdint>
#include <iostream>
int main() {
    int32_t maturity, lockTime;
    int nSpendHeight, coinHeight;
    int64_t adjustCurrentTime;
    uint32_t confirmedTime;
    while (std::cin >> maturity >> lockTime >> nSpendHeight >> coinHeight >> adjustCurrentTime >> confirmedTime) {
        bool isBlockMature = maturity >= 0 && nSpendHeight - coinHeight >= maturity;
        bool isTimeMature = lockTime >= 0 && adjustCurrentTime - confirmedTime >= lockTime;
        bool canSpend = isBlockMature || isTimeMature;
        std::cout << canSpend << '\n';
    }
}
