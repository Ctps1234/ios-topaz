#!/usr/bin/env python3
"""Host-test the read-only WWAN code embedded in patch 0951.

Extracts the actual block from the patch (not a copy), compiles it with small
XNU/QMI stubs, and checks USIM/NAS decoding plus every truncated prefix of the
test frames. ASan/UBSan are on by default; use --no-sanitize if the compiler
does not support them. No modem firmware, device dump or personal identifier
is needed.
"""

import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parent.parent
PATCH = ROOT / "xnu/patches/0951-topaz-wwan-readonly-probe.patch"
MARKER = "read-only WWAN probe"

PREFIX = r'''
#include <cstdint>
#include <cstddef>
#include <cstring>
#include <cstdarg>
#include <cstdio>
#include <vector>
#include <cassert>
#define MIN(a, b) ((a) < (b) ? (a) : (b))
#define QMI_REQ 0
#define QMI_RESP 2
#define QRTR_TYPE_DATA 1
static unsigned g_ms;
static void host_log(const char *, ...) __attribute__((format(printf, 1, 2)));
static void host_log(const char *, ...) {}
#define LOG(fmt, ...) host_log(fmt, ##__VA_ARGS__)
static bool PE_parse_boot_argn(const char *, void *, uint32_t) { return false; }
static void qrtr_send(uint32_t, uint32_t, uint32_t, uint32_t, const void *, uint32_t) {}
static const uint8_t *qm_find(const uint8_t *d, uint32_t len, uint8_t type, uint16_t *tlen)
{
    uint32_t o = 7;
    while (o + 3 <= len) {
        uint16_t l;
        memcpy(&l, d + o + 1, 2);
        if (o + 3 + l > len) break;
        if (d[o] == type) { *tlen = l; return d + o + 3; }
        o += 3 + l;
    }
    return NULL;
}
static void qm_string(const uint8_t *d, uint32_t len, uint8_t type, char *out, size_t max)
{
    uint16_t l = 0;
    const uint8_t *v = qm_find(d, len, type, &l);
    out[0] = 0;
    if (v != NULL) {
        l = (uint16_t)MIN((size_t)l, max - 1);
        memcpy(out, v, l);
        out[l] = 0;
    }
}
'''

TESTS = r'''
static void put16(std::vector<uint8_t> &b, uint16_t v)
{
    b.push_back(v & 0xff); b.push_back(v >> 8);
}
static void tlv(std::vector<uint8_t> &b, uint8_t type, const std::vector<uint8_t> &v)
{
    b.push_back(type); put16(b, (uint16_t)v.size()); b.insert(b.end(), v.begin(), v.end());
}
static std::vector<uint8_t> response(uint16_t msg)
{
    std::vector<uint8_t> b = { QMI_RESP, 1, 0, 0, 0, 0, 0 };
    b[3] = msg & 0xff; b[4] = msg >> 8;
    tlv(b, 2, {0, 0, 0, 0});
    return b;
}
int main()
{
    std::vector<uint8_t> card;
    put16(card, 0xffff); put16(card, 0); put16(card, 0); put16(card, 0);
    card.push_back(1);
    card.insert(card.end(), {1, 0, 0, 0, 0, 1});
    card.insert(card.end(), {2, 7, 0, 0, 0, 0, 2});
    card.insert(card.end(), {1, 2});           // synthetic AID, never logged
    card.insert(card.end(), {0, 0, 3, 10, 0, 3, 10});
    std::vector<uint8_t> uim = response(UIM_GET_CARD_STATUS);
    tlv(uim, 0x10, card);
    wwan_uim_rx(UIM_GET_CARD_STATUS, uim.data(), (uint32_t)uim.size());
    assert(wwan_card_state == 1);
    assert(wwan_app_state == 7);

    for (size_t n = 0; n <= card.size(); n++) {
        wwan_card_status(card.data(), (uint16_t)n);
    }

    std::vector<uint8_t> nas = response(NAS_GET_SERVING_SYS);
    tlv(nas, 0x01, {1, 1, 1, 0, 1, 8});
    std::vector<uint8_t> plmn;
    put16(plmn, 724); put16(plmn, 10);         // synthetic test operator
    plmn.insert(plmn.end(), {4, 'T', 'e', 's', 't'});
    tlv(nas, 0x12, plmn);
    wwan_nas_rx(NAS_GET_SERVING_SYS, nas.data(), (uint32_t)nas.size());
    assert(wwan_reg[0] == 1 && wwan_reg[1] == 1 && wwan_reg[2] == 1 && wwan_reg[4] == 8);
    for (size_t n = 0; n <= nas.size(); n++) {
        wwan_nas_rx(NAS_GET_SERVING_SYS, nas.data(), (uint32_t)n);
    }

    std::printf("WWAN QMI host tests: PASS (USIM, LTE, %zu truncated card frames, %zu truncated NAS frames)\n",
                card.size() + 1, nas.size() + 1);
    return 0;
}
'''


def extract_block():
    lines = PATCH.read_text().splitlines(keepends=True)
    out, inside = [], False
    for line in lines:
        if line.startswith("+/*") and MARKER in line:
            inside = True
        if not inside:
            continue
        if line.startswith("+"):
            out.append(line[1:])
        elif line.startswith(" "):
            break
    block = "".join(out)
    if "wwan_card_status" not in block or "wwan_nas_rx" not in block:
        sys.exit(f"could not extract the WWAN block from {PATCH}")
    return block


def main():
    sanitize = "--no-sanitize" not in sys.argv[1:]
    unknown = set(sys.argv[1:]) - {"--no-sanitize"}
    if unknown:
        sys.exit(f"usage: {Path(__file__).name} [--no-sanitize] (unknown: {', '.join(unknown)})")
    cxx = os.environ.get("CXX", "c++")
    with tempfile.TemporaryDirectory(prefix="topaz-wwan-test-") as tmp:
        tmp = Path(tmp)
        source = tmp / "wwan_host_test.cpp"
        binary = tmp / "wwan_host_test"
        source.write_text(PREFIX + "\n" + extract_block() + "\n" + TESTS)
        command = [
            cxx, "-std=gnu++17", "-O1", "-g", "-Wall", "-Wextra", "-Werror",
            "-Wno-unused-function", "-Wformat=2", "-fno-omit-frame-pointer",
        ]
        if sanitize:
            command += ["-fsanitize=address,undefined"]
        command += [str(source), "-o", str(binary)]
        subprocess.run(command, check=True)
        env = dict(os.environ, ASAN_OPTIONS="halt_on_error=1", UBSAN_OPTIONS="halt_on_error=1")
        subprocess.run([str(binary)], check=True, env=env)


if __name__ == "__main__":
    main()
