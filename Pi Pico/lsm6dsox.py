# lsm6dsox.py -- minimal MicroPython driver for the ST LSM6DSOX accelerometer
# (used on the Adafruit LSM6DSOX breakout). Only the accelerometer is used
# here; the gyro is left powered down since we don't need it.
#
# Register map verified against ST's own lsm6dsox_reg.h and Adafruit's
# CircuitPython driver source for this chip.

from micropython import const
import ustruct
import time

_WHO_AM_I = const(0x0F)
_CHIP_ID = const(0x6C)  # shared by LSM6DSO / LSM6DSO32 / LSM6DSOX -- normal
_CTRL1_XL = const(0x10)
_CTRL3_C = const(0x12)
_OUTX_L_A = const(0x28)

# CTRL1_XL = 0100 00 0 0 -> ODR=104 Hz, FS=+/-2g, LPF2 off.
# (For 52 Hz instead, use 0x30. See the ST datasheet for other rates/ranges --
# the accelerometer full-scale bits are non-standard on this chip: 00=2g,
# 01=16g, 10=4g, 11=8g, so don't guess a byte value without checking.)
_ODR_104HZ_FS_2G = const(0x40)

_SW_RESET = const(0x01)  # CTRL3_C bit 0
_BDU_IF_INC = const(0x44)  # CTRL3_C: BDU=1 (bit6) + IF_INC=1 (bit2)

_SENSITIVITY_2G_MG = 0.061  # mg per LSB at +/-2g full scale (from ST datasheet)


class LSM6DSOX:
    def __init__(self, i2c, address=0x6A):
        self.i2c = i2c
        self.addr = address

        who = self._read(_WHO_AM_I, 1)[0]
        if who != _CHIP_ID:
            raise RuntimeError(
                "LSM6DSOX not found at I2C address 0x%02X (got WHO_AM_I=0x%02X, "
                "expected 0x%02X). Check wiring, and check the I2C address -- "
                "it's 0x6A unless the SDO pin/solder jumper is pulled high, in "
                "which case it's 0x6B." % (address, who, _CHIP_ID)
            )

        # Software reset, then wait for it to self-clear, so we start from a
        # known state regardless of what a previous program left configured.
        self._write(_CTRL3_C, _SW_RESET)
        while self._read(_CTRL3_C, 1)[0] & _SW_RESET:
            time.sleep_ms(1)

        self._write(_CTRL3_C, _BDU_IF_INC)  # block data update + auto-increment
        self._write(_CTRL1_XL, _ODR_104HZ_FS_2G)  # accel on: 104 Hz, +/-2g
        time.sleep_ms(200)  # let the new range/rate settle before trusting reads

    def _read(self, reg, n):
        return self.i2c.readfrom_mem(self.addr, reg, n)

    def _write(self, reg, value):
        self.i2c.writeto_mem(self.addr, reg, bytes([value]))

    def acceleration(self):
        """Return (x, y, z) acceleration in g's."""
        data = self._read(_OUTX_L_A, 6)
        x, y, z = ustruct.unpack("<hhh", data)
        s = _SENSITIVITY_2G_MG / 1000.0
        return (x * s, y * s, z * s)
