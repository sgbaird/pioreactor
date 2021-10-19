# -*- coding: utf-8 -*-
from __future__ import annotations
import time
import json
from threading import Thread
from contextlib import suppress
from typing import Optional


from pioreactor.pubsub import QOS

# from pioreactor.utils import is_pio_job_running
from pioreactor.utils.timing import RepeatedTimer
from pioreactor.background_jobs.subjobs.base import BackgroundSubJob
from pioreactor.background_jobs.led_control import LEDController
from pioreactor.actions.led_intensity import led_intensity, LED_Channel
from pioreactor.automations import events
from pioreactor.utils.timing import current_utc_time


class LEDAutomation(BackgroundSubJob):
    """
    This is the super class that LED automations inherit from. The `run` function will
    execute every `duration` minutes (selected at the start of the program). If `duration` is left
    as None, manually call `run`. This calls the `execute` function, which is what subclasses will define.

    To change setting over MQTT:

    `pioreactor/<unit>/<experiment>/led_automation/<setting>/set` value

    """

    latest_growth_rate = None
    latest_od = None
    latest_od_timestamp = 0
    latest_growth_rate_timestamp = 0
    latest_settings_started_at = current_utc_time()
    latest_settings_ended_at = None
    published_settings = {"duration": {"datatype": "float", "settable": True}}
    edited_channels: set[LED_Channel] = set()
    latest_event: Optional[events.Event] = None

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)

        # this registers all subclasses of LEDAutomation back to LEDController, so the subclass
        # can be invoked in LEDController.
        if hasattr(cls, "key"):
            LEDController.automations[cls.key] = cls

    def __init__(
        self,
        duration: Optional[float] = None,
        skip_first_run: bool = False,
        unit: str = None,
        experiment: str = None,
        **kwargs,
    ):
        super(LEDAutomation, self).__init__(
            job_name="led_automation", unit=unit, experiment=experiment
        )

        self.skip_first_run = skip_first_run

        self.set_duration(duration)
        self.start_passive_listeners()

        self.logger.info(f"Starting {self.__class__.__name__} LED automation.")

    def set_duration(self, duration: Optional[float]):
        if duration:
            self.duration = float(duration)

            with suppress(AttributeError):
                self.run_thread.cancel()

            self.run_thread = RepeatedTimer(
                self.duration * 60,
                self.run,
                job_name=self.job_name,
                run_immediately=(not self.skip_first_run),
            ).start()
        else:
            self.duration = None
            self.run_thread = Thread(target=self.run, daemon=True)
            self.run_thread.start()

    def run(self):
        # TODO: this should be close to or equal to the function in DosingAutomation
        if self.state == self.DISCONNECTED:
            # NOOP
            # we ended early.
            return

        # so, I don't think it's necessary to have LED automations need growth rate and OD

        # elif (self.latest_growth_rate is None) or (self.latest_od is None):
        #    self.logger.debug("Waiting for OD and growth rate data to arrive")
        #    if not is_pio_job_running("od_reading", "growth_rate_calculating"):
        #        self.logger.warning(
        #            "`od_reading` and `growth_rate_calculating` should be running."
        #        )

        #    # solution: wait 25% of duration. If we are still waiting, exit and we will try again next duration.
        #    counter = 0
        #    while (
        #        (self.latest_growth_rate is None) or (self.latest_od is None)
        #    ) and self.state == self.READY:
        #        time.sleep(5)
        #        counter += 1

        #        if self.duration and counter > (self.duration * 60 / 4) / 5:
        #            event = events.NoEvent(
        #                "Waited too long on sensor data. Skipping this run."
        #            )
        #            break
        #    else:
        #        return self.run()

        elif self.state != self.READY:

            # solution: wait 25% of duration. If we are still waiting, exit and we will try again next duration.
            counter = 0
            while self.state != self.READY:
                time.sleep(5)
                counter += 1

                if self.duration and counter > (self.duration * 60 / 4) / 5:
                    event = events.NoEvent(
                        "Waited too long not being in state ready. Am I stuck? Unpause me? Skipping this run."
                    )
                    break
            else:
                return self.run()

        # elif (time.time() - self.most_stale_time) > 5 * 60:
        #    event = events.NoEvent(
        #        "readings are too stale (over 5 minutes old) - are `od_reading` and `growth_rate_calculating` running?"
        #    )
        else:
            try:
                event = self.execute()
            except Exception as e:
                self.logger.debug(e, exc_info=True)
                self.logger.error(e)
                event = events.ErrorOccurred()

        self.logger.info(f"triggered {event}.")
        self.latest_event = event
        return event

    def execute(self) -> events.Event:
        raise NotImplementedError

    @property
    def most_stale_time(self) -> float:
        return min(self.latest_od_timestamp, self.latest_growth_rate_timestamp)

    def set_led_intensity(self, channel: LED_Channel, intensity: float):
        """
        Parameters
        ------------

        Channel:
            The LED channel to modify.
        Intensity: float
            A float between 0-100, inclusive.

        """
        self.edited_channels.add(channel)
        led_intensity(
            channel,
            intensity,
            unit=self.unit,
            experiment=self.experiment,
            pubsub_client=self.pub_client,
            source_of_event=self.job_name,
        )

    ########## Private & internal methods

    def on_disconnect(self):
        self.latest_settings_ended_at = current_utc_time()
        self._send_details_to_mqtt()

        with suppress(AttributeError):
            self.run_thread.cancel()

        for job in self.sub_jobs:
            job.set_state("disconnected")

        for channel in self.edited_channels:
            led_intensity(channel, 0, unit=self.unit, experiment=self.experiment)

        self.clear_mqtt_cache()

    def __setattr__(self, name, value) -> None:
        super(LEDAutomation, self).__setattr__(name, value)
        if name in self.published_settings and name != "state":
            self.latest_settings_ended_at = current_utc_time()
            self._send_details_to_mqtt()
            self.latest_settings_started_at = current_utc_time()
            self.latest_settings_ended_at = None

    def _set_growth_rate(self, message):
        self.previous_growth_rate = self.latest_growth_rate
        self.latest_growth_rate = float(json.loads(message.payload)["growth_rate"])
        self.latest_growth_rate_timestamp = time.time()

    def _set_OD(self, message):

        self.previous_od = self.latest_od
        self.latest_od = float(json.loads(message.payload)["od_filtered"])
        self.latest_od_timestamp = time.time()

    def _send_details_to_mqtt(self):
        self.publish(
            f"pioreactor/{self.unit}/{self.experiment}/{self.job_name}/led_automation_settings",
            json.dumps(
                {
                    "pioreactor_unit": self.unit,
                    "experiment": self.experiment,
                    "started_at": self.latest_settings_started_at,
                    "ended_at": self.latest_settings_ended_at,
                    "automation": self.__class__.__name__,
                    "settings": json.dumps(
                        {
                            attr: getattr(self, attr, None)
                            for attr in self.published_settings
                            if attr != "state"
                        }
                    ),
                }
            ),
            qos=QOS.EXACTLY_ONCE,
        )

    def start_passive_listeners(self):
        self.subscribe_and_callback(
            self._set_OD,
            f"pioreactor/{self.unit}/{self.experiment}/growth_rate_calculating/od_filtered",
        )
        self.subscribe_and_callback(
            self._set_growth_rate,
            f"pioreactor/{self.unit}/{self.experiment}/growth_rate_calculating/growth_rate",
        )


class LEDAutomationContrib(LEDAutomation):
    key: str
