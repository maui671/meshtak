#!/usr/bin/env bash
set -Eeuo pipefail

HAL_BUILD_DIR="${HAL_BUILD_DIR:-/opt/sx1302_hal}"
LIBLORAGW_PATH="/usr/local/lib/libloragw.so"

info(){ echo "[INFO]  $*"; }

if [[ -f "$LIBLORAGW_PATH" ]]; then
    info "libloragw.so already installed at ${LIBLORAGW_PATH}"
    exit 0
fi

info "Cloning SX1302 HAL..."
rm -rf "$HAL_BUILD_DIR"
git clone --depth 1 https://github.com/Lora-net/sx1302_hal.git "$HAL_BUILD_DIR"

info "Configuring HAL source for Meshtastic syncword handling and no-temp-sensor boards..."
python3 - "${HAL_BUILD_DIR}/libloragw/src/loragw_sx1302.c" \
          "${HAL_BUILD_DIR}/libloragw/src/loragw_hal.c" <<'_HALCFG'
import sys
from pathlib import Path

def _rd(path):
    file_path = Path(path)
    if not file_path.is_file():
        print("FAIL: missing " + path)
        sys.exit(1)
    return file_path, file_path.read_text().replace("\r\n", "\n")

sx1302_path, sx1302_source = _rd(sys.argv[1])
hal_path, hal_source = _rd(sys.argv[2])

old_sync = """\
    int err = LGW_REG_SUCCESS;

    /* Multi-SF modem configuration */
    DEBUG_MSG("INFO: configuring LoRa (Multi-SF) SF5->SF6 with syncword PRIVATE (0x12)\\n");
    err |= lgw_reg_w(SX1302_REG_RX_TOP_FRAME_SYNCH0_SF5_PEAK1_POS_SF5, 2);
    err |= lgw_reg_w(SX1302_REG_RX_TOP_FRAME_SYNCH1_SF5_PEAK2_POS_SF5, 4);
    err |= lgw_reg_w(SX1302_REG_RX_TOP_FRAME_SYNCH0_SF6_PEAK1_POS_SF6, 2);
    err |= lgw_reg_w(SX1302_REG_RX_TOP_FRAME_SYNCH1_SF6_PEAK2_POS_SF6, 4);
    if (public == true) {
        DEBUG_MSG("INFO: configuring LoRa (Multi-SF) SF7->SF12 with syncword PUBLIC (0x34)\\n");
        err |= lgw_reg_w(SX1302_REG_RX_TOP_FRAME_SYNCH0_SF7TO12_PEAK1_POS_SF7TO12, 6);
        err |= lgw_reg_w(SX1302_REG_RX_TOP_FRAME_SYNCH1_SF7TO12_PEAK2_POS_SF7TO12, 8);
    } else {
        DEBUG_MSG("INFO: configuring LoRa (Multi-SF) SF7->SF12 with syncword PRIVATE (0x12)\\n");
        err |= lgw_reg_w(SX1302_REG_RX_TOP_FRAME_SYNCH0_SF7TO12_PEAK1_POS_SF7TO12, 2);
        err |= lgw_reg_w(SX1302_REG_RX_TOP_FRAME_SYNCH1_SF7TO12_PEAK2_POS_SF7TO12, 4);
    }

    /* LoRa Service modem configuration */
    if ((public == false) || (lora_service_sf == DR_LORA_SF5) || (lora_service_sf == DR_LORA_SF6)) {
        DEBUG_PRINTF("INFO: configuring LoRa (Service) SF%u with syncword PRIVATE (0x12)\\n", lora_service_sf);
        err |= lgw_reg_w(SX1302_REG_RX_TOP_LORA_SERVICE_FSK_FRAME_SYNCH0_PEAK1_POS, 2);
        err |= lgw_reg_w(SX1302_REG_RX_TOP_LORA_SERVICE_FSK_FRAME_SYNCH1_PEAK2_POS, 4);
    } else {
        DEBUG_PRINTF("INFO: configuring LoRa (Service) SF%u with syncword PUBLIC (0x34)\\n", lora_service_sf);
        err |= lgw_reg_w(SX1302_REG_RX_TOP_LORA_SERVICE_FSK_FRAME_SYNCH0_PEAK1_POS, 6);
        err |= lgw_reg_w(SX1302_REG_RX_TOP_LORA_SERVICE_FSK_FRAME_SYNCH1_PEAK2_POS, 8);
    }

    return err;"""

new_sync = """\
    int err = LGW_REG_SUCCESS;

    uint8_t sw_reg1, sw_reg2;
    if (public == true) {
        sw_reg1 = 6;
        sw_reg2 = 8;
    } else if (lora_service_sf > 12) {
        sw_reg1 = ((lora_service_sf >> 4) & 0x0F) * 2;
        sw_reg2 = (lora_service_sf & 0x0F) * 2;
        DEBUG_PRINTF("INFO: sync cfg 0x%02X -> %u, %u\\n", lora_service_sf, sw_reg1, sw_reg2);
    } else {
        sw_reg1 = 2;
        sw_reg2 = 4;
    }

    err |= lgw_reg_w(SX1302_REG_RX_TOP_FRAME_SYNCH0_SF5_PEAK1_POS_SF5, sw_reg1);
    err |= lgw_reg_w(SX1302_REG_RX_TOP_FRAME_SYNCH1_SF5_PEAK2_POS_SF5, sw_reg2);
    err |= lgw_reg_w(SX1302_REG_RX_TOP_FRAME_SYNCH0_SF6_PEAK1_POS_SF6, sw_reg1);
    err |= lgw_reg_w(SX1302_REG_RX_TOP_FRAME_SYNCH1_SF6_PEAK2_POS_SF6, sw_reg2);

    err |= lgw_reg_w(SX1302_REG_RX_TOP_FRAME_SYNCH0_SF7TO12_PEAK1_POS_SF7TO12, sw_reg1);
    err |= lgw_reg_w(SX1302_REG_RX_TOP_FRAME_SYNCH1_SF7TO12_PEAK2_POS_SF7TO12, sw_reg2);

    err |= lgw_reg_w(SX1302_REG_RX_TOP_LORA_SERVICE_FSK_FRAME_SYNCH0_PEAK1_POS, sw_reg1);
    err |= lgw_reg_w(SX1302_REG_RX_TOP_LORA_SERVICE_FSK_FRAME_SYNCH1_PEAK2_POS, sw_reg2);

    return err;"""

if "sw_reg1" not in sx1302_source:
    if old_sync not in sx1302_source:
        print("FAIL: source mismatch in " + str(sx1302_path))
        sys.exit(1)
    sx1302_path.write_text(sx1302_source.replace(old_sync, new_sync, 1), newline="\n")

patches = [
    ("""\
        /* Find the temperature sensor on the known supported ports */
        for (i = 0; i < (int)(sizeof I2C_PORT_TEMP_SENSOR); i++) {
            ts_addr = I2C_PORT_TEMP_SENSOR[i];
            err = i2c_linuxdev_open(I2C_DEVICE, ts_addr, &ts_fd);
            if (err != LGW_I2C_SUCCESS) {
                printf("ERROR: failed to open I2C for temperature sensor on port 0x%02X\\n", ts_addr);
                return LGW_HAL_ERROR;
            }

            err = stts751_configure(ts_fd, ts_addr);
            if (err != LGW_I2C_SUCCESS) {
                printf("INFO: no temperature sensor found on port 0x%02X\\n", ts_addr);
                i2c_linuxdev_close(ts_fd);
                ts_fd = -1;
            } else {
                printf("INFO: found temperature sensor on port 0x%02X\\n", ts_addr);
                break;
            }
        }
        if (i == sizeof I2C_PORT_TEMP_SENSOR) {
            printf("ERROR: no temperature sensor found.\\n");
            return LGW_HAL_ERROR;
        }""", """\
        /* Find the temperature sensor on the known supported ports */
        for (i = 0; i < (int)(sizeof I2C_PORT_TEMP_SENSOR); i++) {
            ts_addr = I2C_PORT_TEMP_SENSOR[i];
            err = i2c_linuxdev_open(I2C_DEVICE, ts_addr, &ts_fd);
            if (err != LGW_I2C_SUCCESS) {
                printf("WARNING: could not open I2C on port 0x%02X\\n", ts_addr);
                ts_fd = -1;
                continue;
            }

            err = stts751_configure(ts_fd, ts_addr);
            if (err != LGW_I2C_SUCCESS) {
                printf("INFO: no temperature sensor found on port 0x%02X\\n", ts_addr);
                i2c_linuxdev_close(ts_fd);
                ts_fd = -1;
            } else {
                printf("INFO: found temperature sensor on port 0x%02X\\n", ts_addr);
                break;
            }
        }
        if (ts_fd < 0) {
            printf("WARNING: sensor not available, using default\\n");
        }"""),
    ("""\
        case LGW_COM_SPI:
            err = stts751_get_temperature(ts_fd, ts_addr, temperature);
            break;""", """\
        case LGW_COM_SPI:
            if (ts_fd > 0) {
                err = stts751_get_temperature(ts_fd, ts_addr, temperature);
            } else {
                *temperature = 25.0;
                err = LGW_HAL_SUCCESS;
            }
            break;"""),
    ("""\
        DEBUG_MSG("INFO: Closing I2C for temperature sensor\\n");
        x = i2c_linuxdev_close(ts_fd);
        if (x != 0) {
            printf("ERROR: failed to close I2C temperature sensor device (err=%i)\\n", x);
            err = LGW_HAL_ERROR;
        }""", """\
        if (ts_fd > 0) {
            DEBUG_MSG("INFO: Closing I2C for temperature sensor\\n");
            x = i2c_linuxdev_close(ts_fd);
            if (x != 0) {
                printf("ERROR: failed to close I2C temperature sensor device (err=%i)\\n", x);
                err = LGW_HAL_ERROR;
            }
        }"""),
]

for old, new in patches:
    if new in hal_source:
        continue
    if old not in hal_source:
        print("FAIL: source mismatch in " + str(hal_path))
        sys.exit(1)
    hal_source = hal_source.replace(old, new, 1)
hal_path.write_text(hal_source, newline="\n")
_HALCFG

info "Compiling libloragw (this takes a few minutes)..."
cd "$HAL_BUILD_DIR"
make clean 2>/dev/null || true
make -j"$(nproc)"

info "Recompiling with -fPIC for shared library..."
mkdir -p pic_obj

for src in libtools/src/*.c; do
    gcc -c -O2 -fPIC -Wall -Wextra -std=c99 \
        -Ilibtools/inc -Ilibtools \
        "$src" -o "pic_obj/$(basename "${src%.c}.o")"
done

for src in libloragw/src/*.c; do
    gcc -c -O2 -fPIC -Wall -Wextra -std=c99 \
        -Ilibloragw/inc -Ilibloragw -Ilibtools/inc \
        "$src" -o "pic_obj/$(basename "${src%.c}.o")"
done

info "Linking libloragw.so..."
gcc -shared -o libloragw/libloragw.so pic_obj/*.o -lrt -lm -lpthread

info "Installing libloragw.so..."
cp libloragw/libloragw.so "$LIBLORAGW_PATH"
ldconfig
info "libloragw.so installed to ${LIBLORAGW_PATH}"
