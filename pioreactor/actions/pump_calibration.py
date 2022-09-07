# -*- coding: utf-8 -*-
from __future__ import annotations

import time
from typing import Callable
from typing import Optional

import click
from msgspec.json import decode
from msgspec.json import encode

from pioreactor import structs
from pioreactor.actions.pump import add_alt_media
from pioreactor.actions.pump import add_media
from pioreactor.actions.pump import remove_waste
from pioreactor.config import config
from pioreactor.hardware import voltage_in_aux
from pioreactor.logging import create_logger
from pioreactor.pubsub import publish
from pioreactor.utils import local_persistant_storage
from pioreactor.utils import publish_ready_to_disconnected_state
from pioreactor.utils.math_helpers import correlation
from pioreactor.utils.math_helpers import simple_linear_regression_with_forced_nil_intercept
from pioreactor.utils.timing import current_utc_timestamp
from pioreactor.whoami import get_latest_experiment_name
from pioreactor.whoami import get_latest_testing_experiment_name
from pioreactor.whoami import get_unit_name
from pioreactor.whoami import UNIVERSAL_EXPERIMENT


def introduction():
    click.clear()
    click.echo(
        """This routine will calibrate the pumps on your current Pioreactor. You'll need:
    1. A Pioreactor
    2. A vial placed on a scale
    3. A larger container with water
    4. Pumps connected to the correct PWM channel (1, 2, 3, or 4) as determined in your Configurations.
"""
    )


def get_metadata_from_user() -> str:
    with local_persistant_storage("pump_calibrations") as cache:
        while True:
            name = click.prompt("Provide a unique name for this calibration", type=str).strip()
            if name not in cache:
                break
            else:
                if click.confirm("❗️ Name already exists. Do you wish to overwrite?"):
                    break
    return name


def which_pump_are_you_calibrating() -> tuple[str, Callable]:

    with local_persistant_storage("current_pump_calibration") as cache:
        has_media = "media" in cache
        has_waste = "waste" in cache
        has_alt_media = "alt_media" in cache

        if has_media:
            media_timestamp = decode(cache["media"], type=structs.PumpCalibration).timestamp[:10]
        else:
            media_timestamp = ""

        if has_waste:
            waste_timestamp = decode(cache["waste"], type=structs.PumpCalibration).timestamp[:10]
        else:
            waste_timestamp = ""

        if has_alt_media:
            alt_media_timestamp = decode(
                cache["alt_media"], type=structs.PumpCalibration
            ).timestamp[:10]
        else:
            alt_media_timestamp = ""

    r = click.prompt(
        click.style(
            f"""Which pump are you calibrating?
1. Media       {f'[last ran {media_timestamp}]' if has_media else '[missing calibration]'}
2. Alt-media   {f'[last ran {alt_media_timestamp}]' if has_alt_media else '[missing calibration]'}
3. Waste       {f'[last ran {waste_timestamp}]' if has_waste else '[missing calibration]'}
""",
            fg="green",
        ),
        type=click.Choice(["1", "2", "3"]),
        show_choices=True,
    )

    if r == "1":
        if has_media:
            click.confirm(
                click.style("Confirm over-writing existing calibration?", fg="green"),
                abort=True,
                prompt_suffix=" ",
            )
        return ("media", add_media)
    elif r == "2":
        if has_alt_media:
            click.confirm(
                click.style("Confirm over-writing existing calibration?", fg="green"),
                abort=True,
                prompt_suffix=" ",
            )
        return ("alt_media", add_alt_media)
    elif r == "3":
        if has_waste:
            click.confirm(
                click.style("Confirm over-writing existing calibration?", fg="green"),
                abort=True,
                prompt_suffix=" ",
            )
        return ("waste", remove_waste)
    else:
        raise ValueError()


def setup(pump_type: str, execute_pump: Callable, hz: float, dc: float, unit: str) -> None:
    # set up...

    click.clear()
    click.echo()
    click.echo("We need to prime the pump by filling the tubes completely with water.")
    click.echo("1. Fill a container with water.")
    click.echo("2. Place free ends of the tube into the water.")
    click.echo(
        "Make sure the pump's power is connected to "
        + click.style(f"PWM channel {config.get('PWM_reverse', pump_type)}.", bold=True)
    )
    click.echo("We'll run the pumps continuously until the tubes are filled.")
    click.echo(
        click.style("3. Press CTRL+C when the tubes are fully filled with water.", bold=True)
    )

    while not click.confirm(click.style("Ready?", fg="green")):
        pass

    try:
        execute_pump(
            duration=10000,
            source_of_event="pump_calibration",
            unit=get_unit_name(),
            experiment=get_latest_testing_experiment_name(),
            calibration=structs.PumpCalibration(
                name="calibration",
                timestamp=current_utc_timestamp(),
                pump=pump_type,
                duration_=1.0,
                hz=hz,
                dc=dc,
                bias_=0,
                voltage=voltage_in_aux(),
            ),
        )
    except KeyboardInterrupt:
        pass

    click.echo()

    time.sleep(0.5)  # pure UX
    return


def choose_settings() -> tuple[float, float]:
    click.clear()
    click.echo()
    hz = click.prompt(
        click.style("Enter frequency of PWM. [enter] for default 200 hz", fg="green"),
        type=click.FloatRange(0.1 10000),
        default=200,
        show_default=False,
    )
    dc = click.prompt(
        click.style(
            "Enter duty cycle percent as a whole number. [enter] for default 90%", fg="green"
        ),
        type=click.IntRange(0, 100),
        default=90,
        show_default=False,
    )

    return hz, dc


def plot_data(
    x, y, title, x_min=None, x_max=None, interpolation_curve=None, highlight_recent_point=True
):
    import plotext as plt

    plt.clf()

    if interpolation_curve:
        plt.plot(x, [interpolation_curve(x_) for x_ in x], color=204)

    plt.scatter(x, y)

    if highlight_recent_point:
        plt.scatter([x[-1]], [y[-1]], color=204)

    plt.theme("pro")
    plt.title(title)
    plt.plot_size(105, 22)
    plt.xlim(x_min, x_max)
    plt.show()


def run_tests(
    execute_pump: Callable,
    hz: float,
    dc: float,
    min_duration: float,
    max_duration: float,
    pump_type: str,
) -> tuple[list[float], list[float]]:
    click.clear()
    click.echo()
    click.echo("Beginning tests.")

    results: list[float] = []
    durations_to_test = [
        min_duration,
        min_duration * 1.1,
        min_duration * 1.2,
        min_duration * 1.3,
    ] + [max_duration * 0.85, max_duration * 0.90, max_duration * 0.95, max_duration]
    for i, duration in enumerate(durations_to_test):
        while True:
            if i != 0:
                plot_data(
                    durations_to_test[:i],
                    results,
                    title="Pump Calibration (ongoing)",
                    x_min=min_duration,
                    x_max=max_duration,
                )

            if i > 0:
                click.echo(
                    "Remove the water from the measuring container or tare your weighing scale."
                )

            click.echo(
                "We will run the pump for a set amount of time, and you will measure how much liquid is expelled."
            )
            click.echo("Use a small container placed on top of an accurate weighing scale.")
            click.echo(
                "Hold the end of the outflow tube above so the container catches the expelled liquid."
            )
            while not click.confirm(click.style(f"Ready to test {duration:.2f}s?", fg="green")):
                pass

            execute_pump(
                duration=duration,
                source_of_event="pump_calibration",
                unit=get_unit_name(),
                experiment=get_latest_testing_experiment_name(),
                calibration=structs.PumpCalibration(
                    name="",
                    duration_=1.0,
                    pump=pump_type,
                    hz=hz,
                    dc=dc,
                    bias_=0,
                    timestamp=current_utc_timestamp(),
                    voltage=voltage_in_aux(),
                ),
            )

            r = click.prompt(
                click.style("Enter amount of water expelled, or REDO", fg="green"),
                confirmation_prompt=click.style("Repeat for confirmation", fg="green"),
            )
            if r == "REDO":
                click.clear()
                click.echo()
                continue

            try:
                results.append(float(r))
                click.clear()
                click.echo()
                break
            except ValueError:
                click.echo("Not a number - retrying.")

    return durations_to_test, results


def save_results_locally(
    name: str,
    pump_type: str,
    duration_: float,
    bias_: float,
    hz: float,
    dc: float,
    voltage: float,
    durations: list[float],
    volumes: list[float],
    unit: str,
) -> structs.PumpCalibration:
    pump_calibration_result = structs.PumpCalibration(
        name=name,
        timestamp=current_utc_timestamp(),
        pump=pump_type,
        duration_=duration_,
        bias_=bias_,
        hz=hz,
        dc=dc,
        voltage=voltage_in_aux(),
        durations=durations,
        volumes=volumes,
    )

    # save to cache
    with local_persistant_storage("pump_calibrations") as cache:
        cache[name] = encode(pump_calibration_result)

    with local_persistant_storage("current_pump_calibration") as cache:
        cache[pump_type] = encode(pump_calibration_result)

    # send to MQTT
    publish(
        f"pioreactor/{unit}/{UNIVERSAL_EXPERIMENT}/calibrations",
        encode(pump_calibration_result),
    )

    return pump_calibration_result


def pump_calibration(min_duration: float, max_duration: float) -> None:

    unit = get_unit_name()
    experiment = get_latest_experiment_name()

    logger = create_logger("pump_calibration", unit=unit, experiment=experiment)
    logger.info("Starting pump calibration.")

    with publish_ready_to_disconnected_state(unit, experiment, "pump_calibration"):

        introduction()
        name = get_metadata_from_user()
        pump_type, execute_pump = which_pump_are_you_calibrating()

        is_ready = True
        while is_ready:
            hz, dc = choose_settings()
            setup(pump_type, execute_pump, hz, dc, unit)

            is_ready = click.confirm(
                click.style("Do you want to change the frequency or duty cycle?", fg="green"),
                prompt_suffix=" ",
                default=False,
            )

        durations, volumes = run_tests(execute_pump, hz, dc, min_duration, max_duration, pump_type)

        (slope, std_slope), (
            bias,
            std_bias,
        ) = simple_linear_regression_with_forced_nil_intercept(durations, volumes)

        plot_data(
            durations,
            volumes,
            title="Pump Calibration",
            x_min=min_duration,
            x_max=max_duration,
            interpolation_curve=curve_to_callable("poly", [slope, bias]),
            highlight_recent_point=False,
        )

        # check parameters for problems
        if correlation(durations, volumes) < 0:
            logger.warning(
                "Correlation is negative - you probably want to rerun this calibration..."
            )
        if slope / std_slope < 5.0:
            logger.warning(
                "Too much uncertainty in slope - you probably want to rerun this calibration..."
            )

        save_results_locally(
            name=name,
            pump_type=pump_type,
            duration_=slope,
            bias_=bias,
            hz=hz,
            dc=dc,
            voltage=voltage_in_aux(),
            durations=durations,
            volumes=volumes,
            unit=unit,
        )

        logger.debug(f"slope={slope:0.2f} ± {std_slope:0.2f}, bias={bias:0.2f} ± {std_bias:0.2f}")

        logger.debug(
            f"Calibration is best for volumes between {(slope * min_duration + bias):0.1f}mL to {(slope * max_duration + bias):0.1f}mL, but will be okay for slightly outside this range too."
        )
        logger.info("Finished pump calibration.")


def curve_to_callable(curve_type: str, curve_data) -> Optional[Callable]:
    if curve_type == "poly":
        import numpy as np

        def curve_callable(x):
            return np.polyval(curve_data, x)

        return curve_callable

    else:
        return None


def display_current() -> None:
    from pprint import pprint

    with local_persistant_storage("current_pump_calibration") as c:
        for pump in c.keys():
            pump_calibration = decode(c[pump])
            volumes = pump_calibration["volumes"]
            durations = pump_calibration["durations"]
            name, pump = pump_calibration["name"], pump_calibration["pump"]
            plot_data(
                durations,
                volumes,
                title=f"Calibration for {pump} pump",
                highlight_recent_point=False,
                interpolation_curve=curve_to_callable(
                    "poly", [pump_calibration["duration_"], pump_calibration["bias_"]]
                ),
            )
            click.echo(click.style(f"Data for {name}", underline=True, bold=True))
            pprint(pump_calibration)
            click.echo()
            click.echo()
            click.echo()


def change_current(name) -> None:
    try:
        with local_persistant_storage("pump_calibrations") as all_calibrations:
            new_calibration = decode(
                all_calibrations[name], type=structs.PumpCalibration
            )  # decode name from list of all names

        pump_type_from_new_calibration = new_calibration.pump  # retrieve the pump type

        with local_persistant_storage("current_pump_calibration") as current_calibrations:
            old_calibration = decode(
                current_calibrations[pump_type_from_new_calibration], type=structs.PumpCalibration
            )
            current_calibrations[pump_type_from_new_calibration] = encode(new_calibration)
        click.echo(f"Replaced {old_calibration.name} with {new_calibration.name} ✅")
    except Exception as e:
        click.echo(f"Failed to swap. {e}")
        click.Abort()


def list_():
    click.secho(
        f"{'Name':15s} {'Timestamp':35s} {'Pump type':20s}",
        bold=True,
    )
    with local_persistant_storage("pump_calibrations") as c:
        for name in c.keys():
            try:
                cal = decode(c[name], type=structs.PumpCalibration)
                click.secho(
                    f"{cal.name:15s} {cal.timestamp:35s} {cal.pump:20s}",
                )
            except Exception as e:
                raise e


@click.group(invoke_without_command=True, name="pump_calibration")
@click.pass_context
@click.option("--min-duration", type=float)
@click.option("--max-duration", type=float)
def click_pump_calibration(ctx, min_duration, max_duration):
    """
    Calibrate a pump
    """
    if ctx.invoked_subcommand is None:
        if max_duration is None and min_duration is None:
            min_duration, max_duration = 0.45, 1.25
        elif (max_duration is not None) and (min_duration is not None):
            assert min_duration < max_duration, "min_duration >= max_duration"
        else:
            raise ValueError("min_duration and max_duration must both be set.")

        pump_calibration(min_duration, max_duration)


@click_pump_calibration.command(name="display_current")
def click_display_current():
    display_current()


@click_pump_calibration.command(name="change_current")
@click.argument("name", type=click.STRING)
def click_change_current(name):
    change_current(name)


@click_pump_calibration.command(name="list")
def click_list():
    list_()


if __name__ == "__main__":
    click_pump_calibration()
