# -*- coding: utf-8 -*-
import json
import os
import signal
import logging
from collections import defaultdict

import click

from pioreactor.utils.streaming_calculations import ExtendedKalmanFilter
from pioreactor.pubsub import publish, subscribe, subscribe_and_callback, QOS

from pioreactor.whoami import get_unit_from_hostname, get_latest_experiment_name
from pioreactor.config import config
from pioreactor.background_jobs.base import BackgroundJob

JOB_NAME = os.path.splitext(os.path.basename((__file__)))[0]


class GrowthRateCalculator(BackgroundJob):

    editable_settings = []

    def __init__(self, ignore_cache=False, unit=None, experiment=None):
        super(GrowthRateCalculator, self).__init__(
            job_name=JOB_NAME, unit=unit, experiment=experiment
        )
        self.ignore_cache = ignore_cache
        self.initial_growth_rate = self.set_initial_growth_rate()
        self.od_normalization_factors = self.set_od_normalization_factors()
        self.od_variances = self.set_od_variances()
        self.samples_per_minute = 60 * float(
            config["od_config.od_sampling"]["samples_per_second"]
        )
        self.dt = (
            1 / float(config["od_config.od_sampling"]["samples_per_second"]) / 60 / 60
        )
        self.ekf, self.angles = self.initialize_extended_kalman_filter()
        self.start_passive_listeners()

    @property
    def state_(self):
        return self.ekf.state_

    def initialize_extended_kalman_filter(self):
        import numpy as np

        latest_od = subscribe(f"pioreactor/{self.unit}/{self.experiment}/od_raw_batched")
        angles_and_initial_points = self.scale_raw_observations(
            self.json_to_sorted_dict(latest_od.payload)
        )

        initial_state = np.array(
            [*angles_and_initial_points.values(), self.initial_growth_rate]
        )

        d = initial_state.shape[0]

        # empirically selected
        initial_covariance = 0.001 * np.diag(initial_state.tolist()[:-1] + [0.0001])

        OD_process_covariance = self.create_OD_covariance(
            angles_and_initial_points.keys()
        )

        rate_process_variance = (0.0050 * self.dt) ** 2
        process_noise_covariance = np.block(
            [
                [OD_process_covariance, 0 * np.ones((d - 1, 1))],
                [0 * np.ones((1, d - 1)), rate_process_variance],
            ]
        )
        observation_noise_covariance = self.create_obs_noise_covariance(
            angles_and_initial_points.keys()
        )
        return (
            ExtendedKalmanFilter(
                initial_state,
                initial_covariance,
                process_noise_covariance,
                observation_noise_covariance,
                dt=self.dt,
            ),
            angles_and_initial_points.keys(),
        )

    def create_obs_noise_covariance(self, angles):
        import numpy as np

        # if a sensor has X times the variance of the other, we should encode this in the obs. covariance.
        obs_variances = np.array([self.od_variances[angle] for angle in angles])
        obs_variances = obs_variances / obs_variances.min()
        # add a fudge factor
        return 100 * (0.05 * self.dt) ** 2 * np.diag(obs_variances)

    def create_OD_covariance(self, angles):
        import numpy as np

        d = len(angles)
        variances = {
            "135": (1e-2 * self.dt) ** 2,
            "90": (1e-2 * self.dt) ** 2,
            "45": (1e-2 * self.dt) ** 2,
        }

        OD_covariance = 0 * np.ones((d, d))
        for i, a in enumerate(angles):
            for k in variances:
                if a.startswith(k):
                    OD_covariance[i, i] = variances[k]
        return OD_covariance

    def set_initial_growth_rate(self):
        if self.ignore_cache:
            return 1

        message = subscribe(
            f"pioreactor/{self.unit}/{self.experiment}/growth_rate",
            timeout=2,
            qos=QOS.EXACTLY_ONCE,
        )
        if message:
            return float(message.payload)
        else:
            return 0

    def set_od_normalization_factors(self):

        message = subscribe(
            f"pioreactor/{self.unit}/{self.experiment}/od_normalization/median",
            timeout=2,
            qos=QOS.EXACTLY_ONCE,
        )
        if message:
            return self.json_to_sorted_dict(message.payload)
        else:
            return defaultdict(lambda: 1)

    def set_od_variances(self):

        message = subscribe(
            f"pioreactor/{self.unit}/{self.experiment}/od_normalization/variance",
            timeout=2,
            qos=QOS.EXACTLY_ONCE,
        )
        if message:
            return self.json_to_sorted_dict(message.payload)
        else:
            return defaultdict(lambda: 1e-5)

    def update_ekf_variance_after_io_event(self, message):
        self.ekf.scale_OD_variance_for_next_n_steps(2e4, 2 * self.samples_per_minute)

    def scale_raw_observations(self, observations):
        return {
            angle: observations[angle] / self.od_normalization_factors[angle]
            for angle in observations.keys()
        }

    def update_state_from_observation(self, message):
        if self.state != self.READY:
            return
        try:
            observations = self.json_to_sorted_dict(message.payload)
            scaled_observations = self.scale_raw_observations(observations)
            self.ekf.update(list(scaled_observations.values()))

            publish(
                f"pioreactor/{self.unit}/{self.experiment}/growth_rate",
                self.state_[-1],
                retain=True,
            )

            for i, angle_label in enumerate(self.angles):
                publish(
                    f"pioreactor/{self.unit}/{self.experiment}/od_filtered/{angle_label}",
                    self.state_[i],
                )

            return

        except Exception as e:
            self.logger.error(f"failed {str(e)}. Skipping.")
            raise e

    def start_passive_listeners(self):

        # process incoming data
        self.pubsub_clients.append(
            subscribe_and_callback(
                self.update_state_from_observation,
                f"pioreactor/{self.unit}/{self.experiment}/od_raw_batched",
                qos=QOS.EXACTLY_ONCE,
                job_name=self.job_name,
            )
        )
        self.pubsub_clients.append(
            subscribe_and_callback(
                self.update_ekf_variance_after_io_event,
                f"pioreactor/{self.unit}/{self.experiment}/io_events",
                qos=QOS.EXACTLY_ONCE,
                job_name=self.job_name,
            )
        )

    @staticmethod
    def json_to_sorted_dict(json_dict):
        d = json.loads(json_dict)
        return {
            k: float(d[k]) for k in sorted(d, reverse=True) if not k.startswith("180")
        }


def growth_rate_calculating(ignore_cache):
    unit = get_unit_from_hostname()
    experiment = get_latest_experiment_name()

    try:
        calculator = GrowthRateCalculator(  # noqa: F841
            ignore_cache=ignore_cache, unit=unit, experiment=experiment
        )
        while True:
            signal.pause()
    except Exception as e:
        logging.getLogger(JOB_NAME).error(f"failed {str(e)}.")
        raise e


@click.command(name="growth_rate_calculating")
@click.option("--ignore-cache", is_flag=True, help="Ignore the cached growth_rate value")
def click_growth_rate_calculating(ignore_cache):
    # Start the growth rate calculating job
    growth_rate_calculating(ignore_cache)
