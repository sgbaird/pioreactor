# -*- coding: utf-8 -*-
# dacs.py
from __future__ import annotations

import busio  # type: ignore

from pioreactor import hardware
from pioreactor.version import hardware_version_info


class _DAC:

    A = 0
    B = 1
    C = 2
    D = 3

    def power_down(self, channel: int) -> None:
        pass

    def power_up(self, channel: int) -> None:
        pass

    def set_intensity_to(self, channel: int, intensity: float) -> None:
        # float is a value between 0 and 100 inclusive
        pass


class DAC43608_DAC(_DAC):

    A = 8
    B = 9
    C = 10
    D = 11

    def __init__(self) -> None:
        from DAC43608 import DAC43608

        self.dac = DAC43608(address=hardware.DAC)

    def power_down(self, channel: int) -> None:
        self.dac.power_down(channel)  # type: ignore

    def power_up(self, channel: int) -> None:
        self.dac.power_up(channel)  # type: ignore

    def set_intensity_to(self, channel: int, intensity: float) -> None:
        self.dac.set_intensity_to(channel, intensity / 100.0)  # type: ignore


class Pico_DAC(_DAC):
    """
    The DAC is an 8-bit controller implemented in the Pico firmware. See main.c for details.

    """

    A = 0
    B = 1
    C = 2
    D = 3

    def __init__(self) -> None:
        # set up i2c connection to hardware.DAC
        self.i2c = busio.I2C(hardware.SCL, hardware.SDA)

    def power_down(self, channel: int) -> None:
        self.set_intensity_to(channel, 0.0)

    def set_intensity_to(self, channel: int, intensity: float) -> None:
        # to 8 bit integer
        eight_bit = round((intensity / 100.0) * 256.0)
        self.i2c.writeto(hardware.DAC, bytes([0x02, eight_bit]))


DAC = DAC43608_DAC if hardware_version_info <= (1, 0) else Pico_DAC
