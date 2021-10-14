# -*- coding: utf-8 -*-
from pioreactor.automations.temperature.base import TemperatureAutomation
from pioreactor.config import config
from pioreactor.utils.streaming_calculations import PID
from pioreactor.utils import clamp


class DEMA:

    initial_value = 0

    def __init__(self, alpha):
        self.alpha = alpha
        self.value = self.initial_value

    def __call__(self, input_, prev_input_):
        if prev_input_ is not None:
            self.value = (
                self.alpha * (input_ - prev_input_) + (1 - self.alpha) * self.value
            )
        return self.value


class PIDStable(TemperatureAutomation):
    """
    Uses a PID controller to change the DC% to match a target temperature.

    TODO: The PID below uses a derivative term, but we don't have a filter
    filter before or after the derivative. We can improve the output
    by introducing a filter. Unfortunately, the library we are using, simple-pid,
    doesn't have an API for doing this.


    """

    key = "pid_stable"
    published_settings = {
        "target_temperature": {"datatype": "float", "unit": "℃", "settable": True}
    }
    first_update = True

    def __init__(self, target_temperature, **kwargs):
        super(PIDStable, self).__init__(**kwargs)
        assert target_temperature is not None, "target_temperature must be set"
        self.target_temperature = float(target_temperature)

        self.pid = PID(
            Kp=config.getfloat("temperature_automation.pid_stable", "Kp"),
            Ki=config.getfloat("temperature_automation.pid_stable", "Ki"),
            Kd=config.getfloat("temperature_automation.pid_stable", "Kd"),
            setpoint=self.target_temperature,
            unit=self.unit,
            experiment=self.experiment,
            job_name=self.job_name,
            target_name="temperature",
        )

        # d-term is noisy, add this.
        self.pid.pid.add_derivative_hook(DEMA(0.60))

    def execute(self):
        # this runs every time a new temperature reading comes in, including a retained temperature
        if self.first_update:
            self.first_update = False
            # this is the first run of execute. Let's do something
            # smart and look at the delta between the latest_temperature and target_temperature
            # to set a reasonable initial value.
            delta_t = self.target_temperature - self.latest_temperature
            if delta_t <= 0:
                # turn off heater, to drop the temp
                self.update_heater(0)
            else:
                self.update_heater(
                    delta_t * 3.0
                )  # TODO: provide a better linear estimate here, also will fail if using an external PSU
            return  # we'll update with the PID on the next loop.

        output = self.pid.update(
            self.latest_temperature, dt=1
        )  # 1 represents an arbitrary unit of time. The PID values will scale such that 1 makes sense.
        self.update_heater_with_delta(output)
        self.logger.debug(f"delta={output}")
        return

    def set_target_temperature(self, value):
        if float(value) > 50:
            self.logger.warning("Values over 50℃ are not supported. Setting to 50℃.")

        target_temperature = clamp(0, float(value), 50)
        self.target_temperature = target_temperature
        self.pid.set_setpoint(self.target_temperature)
