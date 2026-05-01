#!/usr/bin/env python3
import ctypes
import os
import time
from ctypes import (
    Structure,
    c_uint8,
    c_int8,
    c_uint16,
    c_uint32,
    c_char_p,
    c_int,
    POINTER,
    byref,
)


LGW_PATHS = [
    "/usr/local/lib/libloragw.so",
    "/opt/sx1302_hal/libloragw/libloragw.so",
]


TX_MODE_IMMEDIATE = 0
MOD_LORA = 0x10

BW_125KHZ = 0x03
BW_250KHZ = 0x04
BW_500KHZ = 0x05

CR_LORA_4_5 = 0x01
CR_LORA_4_6 = 0x02
CR_LORA_4_7 = 0x03
CR_LORA_4_8 = 0x04

STAT_UNDEFINED = 0
STAT_SCHEDULED = 1
STAT_EMITTED = 2
STAT_ABORTED = 3


class LGWPktTx(Structure):
    _fields_ = [
        ("freq_hz", c_uint32),
        ("tx_mode", c_uint8),
        ("count_us", c_uint32),
        ("rf_chain", c_uint8),
        ("rf_power", c_int8),
        ("modulation", c_uint8),
        ("bandwidth", c_uint8),
        ("datarate", c_uint32),
        ("coderate", c_uint8),
        ("invert_pol", c_uint8),
        ("f_dev", c_uint8),
        ("preamble", c_uint16),
        ("no_crc", c_uint8),
        ("no_header", c_uint8),
        ("size", c_uint16),
        ("payload", c_uint8 * 256),
    ]


class SX1302Tx:
    def __init__(self, lib_path=None):
        self.lib_path = lib_path or self._find_lib()
        self.lib = ctypes.CDLL(self.lib_path)

        self.lib.lgw_send.argtypes = [POINTER(LGWPktTx)]
        self.lib.lgw_send.restype = c_int

        self.lib.lgw_status.argtypes = [c_uint8, POINTER(c_uint8)]
        self.lib.lgw_status.restype = c_int

    def _find_lib(self):
        for path in LGW_PATHS:
            if os.path.exists(path):
                return path
        raise FileNotFoundError("libloragw.so not found")

    def send_raw_lora(
        self,
        payload: bytes,
        freq_hz: int = 906875000,
        rf_chain: int = 0,
        power_dbm: int = 22,
        spreading_factor: int = 11,
        bandwidth_khz: int = 250,
        coding_rate: str = "4/8",
        preamble: int = 16,
        invert_pol: bool = False,
        no_crc: bool = False,
        no_header: bool = False,
    ):
        if len(payload) > 255:
            raise ValueError("Payload too large; max 255 bytes")

        pkt = LGWPktTx()
        pkt.freq_hz = int(freq_hz)
        pkt.tx_mode = TX_MODE_IMMEDIATE
        pkt.count_us = 0
        pkt.rf_chain = int(rf_chain)
        pkt.rf_power = int(power_dbm)
        pkt.modulation = MOD_LORA
        pkt.bandwidth = self._bandwidth_value(bandwidth_khz)
        pkt.datarate = int(spreading_factor)
        pkt.coderate = self._coding_rate_value(coding_rate)
        pkt.invert_pol = 1 if invert_pol else 0
        pkt.f_dev = 0
        pkt.preamble = int(preamble)
        pkt.no_crc = 1 if no_crc else 0
        pkt.no_header = 1 if no_header else 0
        pkt.size = len(payload)

        for i, b in enumerate(payload):
            pkt.payload[i] = b

        rc = self.lib.lgw_send(byref(pkt))
        if rc != 0:
            raise RuntimeError(f"lgw_send failed rc={rc}")

        return True

    def wait_for_tx_done(self, rf_chain: int = 0, timeout: float = 3.0):
        start = time.time()
        status = c_uint8(STAT_UNDEFINED)

        while time.time() - start < timeout:
            rc = self.lib.lgw_status(c_uint8(rf_chain), byref(status))
            if rc != 0:
                raise RuntimeError(f"lgw_status failed rc={rc}")

            if status.value in (STAT_EMITTED, STAT_ABORTED):
                return status.value

            time.sleep(0.05)

        return status.value

    @staticmethod
    def _bandwidth_value(bw):
        bw = int(bw)
        if bw == 125:
            return BW_125KHZ
        if bw == 250:
            return BW_250KHZ
        if bw == 500:
            return BW_500KHZ
        raise ValueError(f"Unsupported bandwidth: {bw}")

    @staticmethod
    def _coding_rate_value(cr):
        cr = str(cr).strip()
        return {
            "4/5": CR_LORA_4_5,
            "4/6": CR_LORA_4_6,
            "4/7": CR_LORA_4_7,
            "4/8": CR_LORA_4_8,
        }.get(cr, CR_LORA_4_8)


def main():
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--hex", required=True, help="Payload hex string")
    parser.add_argument("--freq", type=int, default=906875000)
    parser.add_argument("--sf", type=int, default=11)
    parser.add_argument("--bw", type=int, default=250)
    parser.add_argument("--cr", default="4/8")
    parser.add_argument("--power", type=int, default=22)
    parser.add_argument("--rf-chain", type=int, default=0)
    args = parser.parse_args()

    tx = SX1302Tx()
    payload = bytes.fromhex(args.hex)

    tx.send_raw_lora(
        payload=payload,
        freq_hz=args.freq,
        rf_chain=args.rf_chain,
        power_dbm=args.power,
        spreading_factor=args.sf,
        bandwidth_khz=args.bw,
        coding_rate=args.cr,
    )

    status = tx.wait_for_tx_done(args.rf_chain)
    print(f"tx_status={status}")


if __name__ == "__main__":
    main()
