# -*- coding: utf-8 -*-
"""
Continuously take an optical density reading (more accurately: a backscatter reading, which is a proxy for OD).
This script is designed to run in a background process and push data to MQTT.

>>> morbidostat od_reading --background


Topics published to

    morbidostat/<unit>/<experiment>/od_raw/<angle>/<label>

Ex:

    morbidostat/1/trial15/od_raw/135/A


Also published to

    morbidostat/<unit>/<experiment>/od_raw_batched



"""
import time
import json
import os
import string
from collections import Counter

import click
from adafruit_ads1x15.analog_in import AnalogIn
import adafruit_ads1x15.ads1115 as ADS
import board
import busio

from morbidostat.utils.streaming_calculations import MovingStats
from morbidostat.utils import log_start, log_stop
from morbidostat.whoami import unit, experiment
from morbidostat.config import config
from morbidostat.pubsub import publish, subscribe_and_callback
from morbidostat.utils.timing import every


ADS_GAIN_THRESHOLDS = {
    2 / 3: (4.096, 6.144),
    1: (2.048, 4.096),
    2: (1.024, 2.048),
    4: (0.512, 1.024),
    8: (0.256, 0.512),
    16: (-1, 0.256),
}

JOB_NAME = os.path.splitext(os.path.basename((__file__)))[0]


class ODReader:
    """
    Parameters
    -----------

    od_channels: list of (label, ADS channel), ex: [("90/A", 0), ("90/B", 1), ...]

    """

    def __init__(self, od_channels, ads, unit=None, experiment=None, verbose=0):
        self.unit = unit
        self.experiment = experiment
        self.verbose = verbose
        self.ma = MovingStats(lookback=20)
        self.ads = ads
        self.od_channels = {}

        for (label, channel) in od_channels:
            ai = AnalogIn(self.ads, getattr(ADS, "P" + channel))
            self.od_channels[label] = ai

        self.pause = 0
        self.start_passive_listeners()

    def take_reading(self, counter=None):
        while self.pause == 1:
            time.sleep(0.5)

        try:
            raw_signals = {}
            for (angle_label, ads_channel) in self.od_channels.items():
                raw_signal_ = ads_channel.voltage
                publish(f"morbidostat/{self.unit}/{self.experiment}/od_raw/{angle_label}", raw_signal_, verbose=self.verbose)
                raw_signals[angle_label] = raw_signal_

            # publish the batch of data, too, for growth reading
            publish(f"morbidostat/{self.unit}/{self.experiment}/od_raw_batched", json.dumps(raw_signals), verbose=self.verbose)

            # the max signal should determine the board's gain
            self.ma.update(max(raw_signals.values()))

            # check if using correct gain
            if counter % 20 == 0 and self.ma.mean is not None:
                for gain, (lb, ub) in ADS_GAIN_THRESHOLDS.items():
                    if (0.85 * lb <= self.ma.mean < 0.85 * ub) and (self.ads.gain != gain):
                        self.ads.gain = gain
                        publish(
                            f"morbidostat/{self.unit}/{self.experiment}/log",
                            f"ADC gain updated to {self.ads.gain}.",
                            verbose=self.verbose,
                        )
                        break
        except OSError as e:
            # just pause, not sure why this happens when add_media or remove_waste are called.
            publish(
                f"morbidostat/{self.unit}/{self.experiment}/error_log",
                f"[od_reading] failed with {str(e)}. Attempting to continue.",
                verbose=self.verbose,
            )
            time.sleep(5.0)
        except Exception as e:
            publish(
                f"morbidostat/{self.unit}/{self.experiment}/error_log", f"[od_reading] failed with {str(e)}", verbose=self.verbose
            )
            raise e

    def set_pause(self, message):
        self.pause = int(message.payload)
        publish(f"morbidostat/{self.unit}/{self.experiment}/log", f"[od_reading]: pause={self.pause}", verbose=self.verbose)

    def start_passive_listeners(self):
        subscribe_and_callback(self.set_pause, f"morbidostat/{self.unit}/{self.experiment}/{JOB_NAME}/pause")


@log_start(unit, experiment)
@log_stop(unit, experiment)
def od_reading(od_angle_channel, verbose):
    angle_counter = Counter()
    od_channels = []
    for input_ in od_angle_channel:
        angle, channel = input_.split(",")

        # We split input of the form ["135,x", "135,y", "90,z"] into the form
        # "135/A", "135/B", "90/A"
        angle_counter.update([angle])
        angle_label = str(angle) + "/" + string.ascii_uppercase[angle_counter[angle] - 1]

        od_channels.append((angle_label, channel))

    sampling_rate = 1 / float(config["od_sampling"]["samples_per_second"])

    i2c = busio.I2C(board.SCL, board.SDA)
    ads = ADS.ADS1115(i2c, gain=2)  # we change the gain dynamically later

    yield from every(sampling_rate, ODReader(od_channels, ads, unit=unit, experiment=experiment, verbose=verbose).take_reading)


@click.command()
@click.option(
    "--od-angle-channel",
    multiple=True,
    default=list(config["od_config"].values()),
    type=click.STRING,
    help="""
pair of angle,channel for optical density reading. Can be invoked multiple times. Ex:

--od-angle-channel 135,0 --od-angle-channel 90,1 --od-angle-channel 45,2

""",
)
@click.option(
    "--verbose", "-v", count=True, help="print to std. out (may be redirected to morbidostat.log). Increasing values log more."
)
def click_od_reading(od_angle_channel, verbose):
    reader = od_reading(od_angle_channel, verbose)
    while True:
        next(reader)


if __name__ == "__main__":
    click_od_reading()
