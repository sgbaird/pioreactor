"""
Continuously monitor the bioreactor and take action. This is the core of the io algorithm
"""
import time
import signal
import sys
import threading

import numpy as np
import pandas as pd
from scipy.optimize import curve_fit
import paho.mqtt.subscribe as subscribe

import click
import board
import busio

from morbidostat.actions.add_media import add_media
from morbidostat.actions.remove_waste import remove_waste
from morbidostat.actions.add_alt_media import add_alt_media
from morbidostat.utils.timing_and_threading import every
from morbidostat.utils.pubishing import publish
from morbidostat.utils import config


class ControlAlgorithm:

    latest_rate = 0
    latest_od = 0

    def run(self):
        self.set_OD_measurements()
        self.execute()
        return

    def set_OD_measurements(self):
        self.previous_rate, self.previous_od = self.latest_rate, self.latest_od

        self.latest_rate = float(subscribe.simple(f"morbidostat/{self.unit}/growth_rate").payload)
        self.latest_od = float(subscribe.simple(f"morbidostat/{self.unit}/od_filtered").payload)
        return


######################
# modes of operation
######################


class Silent(ControlAlgorithm):
    def __init__(self, **kwargs):
        return

    def execute(self, *args, **kwargs):
        return


class Turbidostat(ControlAlgorithm):
    """
    turbidostat mode - try to keep cell density constant
    """

    def __init__(self, target_od=None, unit=None, volume=None, **kwargs):
        self.target_od = target_od
        self.unit = unit
        self.volume = volume

    def execute(self):
        if self.latest_od > self.target_od and self.latest_rate > 0:
            publish(f"morbidostat/{self.unit}/log", "Monitor triggered dilution event.")
            time.sleep(0.2)
            remove_waste(self.volume, self.unit)
            time.sleep(0.2)
            add_media(self.volume, self.unit)
        else:
            publish(f"morbidostat/{self.unit}/log", "Monitor triggered no event.")
        return


class Morbidostat(ControlAlgorithm):
    def __init__(self, target_od=None, unit=None, volume=None, **kwargs):
        self.target_od = target_od
        self.unit = unit
        self.volume = volume
        self.latest_alt_media_fraction = 0.0

    def execute(self):
        """
        morbidostat mode - keep cell density below and threshold using chemical means. The conc.
        of the chemical is diluted slowly over time, allowing the microbes to recover.
        """
        if self.latest_od > self.target_od and self.latest_od > self.previous_od:
            # if we are above the threshold, and growth rate is greater than dilution rate
            # the second condition is an approximation of this.
            publish(f"morbidostat/{self.unit}/log", "Monitor triggered alt media event.")
            time.sleep(0.2)
            remove_waste(self.volume, self.unit)
            time.sleep(0.2)
            add_alt_media(self.volume, self.unit)
            self.update_alt_media_fraction(media_delta=0, alt_media_delta=self.volume)
        else:
            publish(f"morbidostat/{self.unit}/log", "Monitor triggered dilution event.")
            time.sleep(0.2)
            remove_waste(self.volume, self.unit)
            time.sleep(0.2)
            add_media(self.volume, self.unit)
            self.update_alt_media_fraction(media_delta=self.volume, alt_media_delta=0)
        return

    def update_alt_media_fraction(self, media_delta, alt_media_delta):
        # should this live here, or in another listener...
        vial_volume = 12
        total_delta = media_delta + alt_media_delta

        # current mL
        alt_media_ml = vial_volume * self.latest_alt_media_fraction
        media_ml = vial_volume * (1 - self.latest_alt_media_fraction)

        # remove
        alt_media_ml = alt_media_ml * (1 - total_delta / vial_volume)
        media_ml = media_ml * (1 - total_delta / vial_volume)

        # add (alt) media
        alt_media_ml = alt_media_ml + alt_media_delta
        media_ml = media_ml + media_delta

        self.latest_alt_media_fraction = alt_media_ml / vial_volume

        publish(
            f"morbidostat/{self.unit}/alt_media_fraction", self.latest_alt_media_fraction,
        )

        return


@click.command()
@click.option(
    "--mode",
    default="silent",
    help="set the mode of the system: turbidostat, morbidostat, silent, etc.",
)
@click.option("--target_od", default=None, type=float)
@click.option("--unit", default="1", help="The morbidostat unit")
@click.option("--duration", default=30, help="Time, in minutes, between every monitor check")
@click.option("--volume", default=0.25, help="the volume to exchange, mL")
def monitoring(mode, target_od, unit, duration, volume):
    def terminate(*args):
        publish(f"morbidostat/{unit}/log", f"Monitor terminated.")
        sys.exit()

    signal.signal(signal.SIGTERM, terminate)

    algorithms = {
        "silent": Silent(),
        "morbidostat": Morbidostat(unit=unit, volume=volume, target_od=target_od),
        "turbidostat": Turbidostat(unit=unit, volume=volume, target_od=target_od),
    }

    assert mode in algorithms.keys()
    assert duration > 10

    publish(
        f"morbidostat/{unit}/log",
        f"starting {mode} with {duration}min intervals, target OD {target_od}V, volume {volume}mL.",
    )

    ##############################
    # main loop
    ##############################
    try:
        every(duration * 60, algorithms[mode].run)
    except Exception as e:
        publish(f"morbidostat/{unit}/error_log", f"Monitor failed: {str(e)}")
        publish(f"morbidostat/{unit}/log", f"Monitor failed: {str(e)}")


if __name__ == "__main__":
    monitoring()
