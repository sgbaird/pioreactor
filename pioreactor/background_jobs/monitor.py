# -*- coding: utf-8 -*-
from time import sleep
from json import dumps, loads
from datetime import datetime
from enum import IntEnum

import click

from pioreactor.whoami import (
    get_unit_name,
    UNIVERSAL_EXPERIMENT,
    UNIVERSAL_IDENTIFIER,
    is_testing_env,
    get_latest_experiment_name,
    am_I_leader,
)
from pioreactor.background_jobs.base import BackgroundJob, NiceMixin
from pioreactor.utils.timing import RepeatedTimer
from pioreactor.pubsub import QOS
from pioreactor.hardware_mappings import (
    PCB_LED_PIN as LED_PIN,
    PCB_BUTTON_PIN as BUTTON_PIN,
)
from pioreactor.utils import pio_jobs_running, local_persistant_storage
from pioreactor.utils.gpio_helpers import GPIO_states, set_gpio_availability
from pioreactor.version import __version__


class ErrorCode(IntEnum):

    MQTT_CLIENT_NOT_CONNECTED_TO_LEADER_ERROR_CODE = 2
    DISK_IS_ALMOST_FULL_ERROR_CODE = 3


class Monitor(NiceMixin, BackgroundJob):
    """
    This job starts at Rpi startup, and isn't connected to any experiment. It has the following roles:

     1. Reports metadata (voltage, CPU usage, etc.) about the Rpi / Pioreactor to the leader
     2. Controls the LED / Button interaction
     3. Correction after a restart
     4. Check database backup if leader
     5. Use the LED blinks to report error codes to the user, see ErrorCode
     6. Listens to MQTT for job to start, on the topic
         pioreactor/{unit}/$experiment/run/{job_name}   json-encoded args as message

    """

    def __init__(self, unit, experiment):
        super().__init__(job_name="monitor", unit=unit, experiment=experiment)

        self.logger.debug(f"PioreactorApp version: {__version__}")

        # set up a self check function to periodically check vitals and log them
        self.self_check_thread = RepeatedTimer(
            12 * 60 * 60, self.self_checks, job_name=self.job_name, run_immediately=True
        ).start()

        # set up GPIO for accessing the button
        self.setup_GPIO()

        self.start_passive_listeners()

    def setup_GPIO(self):
        set_gpio_availability(BUTTON_PIN, GPIO_states.GPIO_UNAVAILABLE)
        set_gpio_availability(LED_PIN, GPIO_states.GPIO_UNAVAILABLE)

        import RPi.GPIO as GPIO

        # I am hiding all the slow imports, but in this case, I need GPIO module
        # in many functions.
        self.GPIO = GPIO

        self.GPIO.setmode(GPIO.BCM)
        self.GPIO.setup(BUTTON_PIN, GPIO.IN, pull_up_down=self.GPIO.PUD_DOWN)
        self.GPIO.setup(LED_PIN, GPIO.OUT)
        self.GPIO.add_event_detect(
            BUTTON_PIN, self.GPIO.RISING, callback=self.button_down_and_up
        )

    def self_checks(self):
        # watch for undervoltage problems
        self.check_for_power_problems()

        # report on CPU usage, memory, disk space
        self.publish_self_statistics()

        if am_I_leader():
            # report on last database backup, if leader
            self.check_for_last_backup()

        if not am_I_leader():
            # check for MQTT connection to leader
            self.check_for_mqtt_connection_to_leader()

    def check_for_mqtt_connection_to_leader(self):
        # TODO test this
        if (not self.pub_client.is_connected()) or (not self.sub_client.is_connected()):
            self.logger.warning(
                "MQTT client(s) are not connected to leader."
            )  # remember, this doesn't go to leader...

            # should this be in a thread?
            self.flicker_led_error_code(
                ErrorCode.MQTT_CLIENT_NOT_CONNECTED_TO_LEADER_ERROR_CODE
            )

    def check_for_last_backup(self):

        with local_persistant_storage("database_backups") as cache:
            if cache.get("latest_backup_timestamp"):
                latest_backup_at = datetime.strptime(
                    cache["latest_backup_timestamp"].decode("utf-8"),
                    "%Y-%m-%dT%H:%M:%S.%fZ",
                )

                if (datetime.utcnow() - latest_backup_at).days > 30:
                    self.logger.warning("Database hasn't been backed up in over 30 days.")

    def check_state_of_jobs_on_machine(self):
        """
        This compares jobs that are current running on the machine, vs
        what MQTT says. In the case of a restart on leader, MQTT can get out
        of sync. We only need to run this check on startup.

        See answer here: https://iot.stackexchange.com/questions/5784/does-mosquito-broker-persist-lwt-messages-to-disk-so-they-may-be-recovered-betw
        """
        latest_exp = get_latest_experiment_name()
        whats_running = pio_jobs_running()

        def check_against_processes_running(msg):
            job = msg.topic.split("/")[3]
            if (msg.payload.decode() in [self.READY, self.INIT, self.SLEEPING]) and (
                job not in whats_running
            ):
                self.publish(
                    f"pioreactor/{self.unit}/{latest_exp}/{job}/$state",
                    self.LOST,
                    retain=True,
                )
                self.logger.debug(f"Manually changing {job} state in MQTT.")

        self.subscribe_and_callback(
            check_against_processes_running,
            f"pioreactor/{self.unit}/{latest_exp}/+/$state",
        )

        # let the above code run...
        sleep(2.5)

        # unsubscribe
        self.sub_client.message_callback_remove(
            f"pioreactor/{self.unit}/{latest_exp}/+/$state"
        )
        self.sub_client.unsubscribe(f"pioreactor/{self.unit}/{latest_exp}/+/$state")

        return

    def on_ready(self):
        self.flicker_led_response_okay()

        # we can delay this check until ready.
        self.check_state_of_jobs_on_machine()

        self.logger.info(f"{self.unit} online and ready.")

    def on_disconnect(self):
        self.GPIO.cleanup(LED_PIN)
        self.GPIO.cleanup(BUTTON_PIN)
        set_gpio_availability(BUTTON_PIN, GPIO_states.GPIO_AVAILABLE)
        set_gpio_availability(LED_PIN, GPIO_states.GPIO_AVAILABLE)

    def led_on(self):
        self.GPIO.output(LED_PIN, self.GPIO.HIGH)

    def led_off(self):
        self.GPIO.output(LED_PIN, self.GPIO.LOW)

    def button_down_and_up(self, *args):
        # Warning: this might be called twice: See "Switch debounce" in https://sourceforge.net/p/raspberry-gpio-python/wiki/Inputs/
        # don't put anything that is not idempotent in here.
        self.publish(
            f"pioreactor/{self.unit}/{self.experiment}/{self.job_name}/button_down",
            1,
            qos=QOS.AT_LEAST_ONCE,
        )

        self.led_on()

        self.logger.debug("Pushed tactile button")

        while self.GPIO.input(BUTTON_PIN) == self.GPIO.HIGH:

            # we keep sending it because the user may change the webpage.
            self.publish(
                f"pioreactor/{self.unit}/{self.experiment}/{self.job_name}/button_down", 1
            )
            sleep(0.25)

        self.publish(
            f"pioreactor/{self.unit}/{self.experiment}/{self.job_name}/button_down",
            0,
            qos=QOS.AT_LEAST_ONCE,
        )
        self.led_off()

    def check_for_power_problems(self):
        """
        Note: `get_throttled` feature isn't available on the Rpi Zero

        Sourced from https://github.com/raspberrypi/linux/pull/2397
         and https://github.com/N2Github/Proje
        """

        def status_to_human_readable(status):
            hr_status = []

            # if status & 0x40000:
            #     hr_status.append("Throttling has occurred.")
            # if status & 0x20000:
            #     hr_status.append("ARM frequency capping has occurred.")
            # if status & 0x10000:
            #     hr_status.append("Undervoltage has occurred.")
            if status & 0x4:
                hr_status.append("Active throttling")
            if status & 0x2:
                hr_status.append("Active ARM frequency capped")
            if status & 0x1:
                hr_status.append("Active undervoltage")

            hr_status.append(
                "Suggestion: use a larger external power supply. See docs at: https://pioreactor.com/pages/using-an-external-power-supply"
            )
            return ". ".join(hr_status)

        def currently_throttling(status):
            return (status & 0x2) or (status & 0x1) or (status & 0x4)

        def non_ignorable_status(status):
            return (status & 0x1) or (status & 0x4)

        if is_testing_env():
            return

        with open("/sys/devices/platform/soc/soc:firmware/get_throttled") as file:
            status = int(file.read(), 16)

        if not currently_throttling(status):
            self.logger.debug("Power status okay.")
        else:
            self.logger.debug(f"Power status: {status_to_human_readable(status)}")

    def publish_self_statistics(self):
        import psutil

        if is_testing_env():
            return

        disk_usage_percent = round(psutil.disk_usage("/").percent)
        cpu_usage_percent = round(
            psutil.cpu_percent()
        )  # TODO: this is a noisy process, and we should average it over a small window.
        available_memory_percent = round(
            100 * psutil.virtual_memory().available / psutil.virtual_memory().total
        )

        with open("/sys/class/thermal/thermal_zone0/temp", "r") as f:
            cpu_temperature_celcius = round(int(f.read().strip()) / 1000)

        if disk_usage_percent <= 80:
            self.logger.debug(f"Disk space at {disk_usage_percent}%.")
        else:
            # TODO: add documentation to clear disk space.
            self.logger.warning(f"Disk space at {disk_usage_percent}%.")
            self.flicker_led_error_code(ErrorCode.DISK_IS_ALMOST_FULL_ERROR_CODE)

        if cpu_usage_percent <= 75:
            self.logger.debug(f"CPU usage at {cpu_usage_percent}%.")
        else:
            # TODO: add documentation
            self.logger.warning(f"CPU usage at {cpu_usage_percent}%.")

        if available_memory_percent >= 20:
            self.logger.debug(f"Available memory at {available_memory_percent}%.")
        else:
            # TODO: add documentation
            self.logger.warning(f"Available memory at {available_memory_percent}%.")

        if cpu_temperature_celcius <= 70:
            self.logger.debug(f"CPU temperature at {cpu_temperature_celcius} ℃.")
        else:
            # TODO: add documentation
            self.logger.warning(f"CPU temperature at {cpu_temperature_celcius} ℃.")

        self.publish(
            f"pioreactor/{self.unit}/{self.experiment}/{self.job_name}/computer_statistics",
            dumps(
                {
                    "disk_usage_percent": disk_usage_percent,
                    "cpu_usage_percent": cpu_usage_percent,
                    "available_memory_percent": available_memory_percent,
                    "cpu_temperature_celcius": cpu_temperature_celcius,
                }
            ),
        )

    def flicker_led_response_okay(self, *args):
        for _ in range(4):

            self.led_on()
            sleep(0.14)
            self.led_off()
            sleep(0.14)
            self.led_on()
            sleep(0.14)
            self.led_off()
            sleep(0.45)

    def flicker_led_error_code(self, error_code):
        while True:
            for _ in range(error_code):
                self.led_on()
                sleep(0.5)
                self.led_off()
                sleep(0.5)

            sleep(5)

    def run_job_on_machine(self, msg):

        import subprocess
        from shlex import (
            quote,
        )  # https://docs.python.org/3/library/shlex.html#shlex.quote

        job_name = quote(msg.topic.split("/")[-1])
        payload = loads(msg.payload)

        prefix = ["nohup"]
        core_command = ["pio", "run", job_name]
        args = sum(
            [
                [f"--{quote(key).replace('_', '-')}", quote(str(value))]
                for key, value in payload.items()
            ],
            [],
        )
        suffix = [">/dev/null", "2>&1", "&"]

        command = " ".join((prefix + core_command + args + suffix))

        self.logger.debug(f"Running `{command}` from monitor job.")

        subprocess.run(command, shell=True)

    def start_passive_listeners(self):
        self.subscribe_and_callback(
            self.flicker_led_response_okay,
            f"pioreactor/{self.unit}/+/{self.job_name}/flicker_led_response_okay",
            qos=QOS.AT_LEAST_ONCE,
        )

        # one can also start jobs via MQTT, using the following topics.
        # The message provided is options the the command line.
        self.subscribe_and_callback(
            self.run_job_on_machine,
            f"pioreactor/{self.unit}/{UNIVERSAL_EXPERIMENT}/run/+",
        )

        self.subscribe_and_callback(
            self.run_job_on_machine,
            f"pioreactor/{UNIVERSAL_IDENTIFIER}/{UNIVERSAL_EXPERIMENT}/run/+",
        )


@click.command(name="monitor")
def click_monitor():
    """
    Monitor and report metadata on the unit.
    """
    job = Monitor(unit=get_unit_name(), experiment=UNIVERSAL_EXPERIMENT)
    job.block_until_disconnected()
