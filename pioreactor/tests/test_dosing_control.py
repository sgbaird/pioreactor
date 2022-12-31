# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import time
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from threading import Timer
from typing import Any

import pytest
from click.testing import CliRunner
from msgspec.json import encode

from pioreactor import exc
from pioreactor import pubsub
from pioreactor import structs
from pioreactor.automations import DosingAutomationJob
from pioreactor.automations import events
from pioreactor.automations.dosing.base import AltMediaCalculator
from pioreactor.automations.dosing.continuous_cycle import ContinuousCycle
from pioreactor.automations.dosing.morbidostat import Morbidostat
from pioreactor.automations.dosing.pid_morbidostat import PIDMorbidostat
from pioreactor.automations.dosing.silent import Silent
from pioreactor.automations.dosing.turbidostat import Turbidostat
from pioreactor.background_jobs.dosing_control import DosingController
from pioreactor.background_jobs.dosing_control import start_dosing_control
from pioreactor.utils import local_persistant_storage
from pioreactor.utils.timing import current_utc_datetime
from pioreactor.whoami import get_unit_name


unit = get_unit_name()


def pause(n=1) -> None:
    # to avoid race conditions when updating state
    time.sleep(n * 0.5)


def setup_function() -> None:
    with local_persistant_storage("current_pump_calibration") as cache:
        cache["media"] = encode(
            structs.MediaPumpCalibration(
                name="setup_function",
                duration_=1.0,
                bias_=0.0,
                dc=60,
                hz=100,
                timestamp=datetime(2010, 1, 1, tzinfo=timezone.utc),
                voltage=-1.0,
                pump="media",
                durations=[0, 1],
                volumes=[0, 1.5],
            )
        )
        cache["alt_media"] = encode(
            structs.AltMediaPumpCalibration(
                name="setup_function",
                duration_=1.0,
                bias_=0,
                dc=60,
                hz=100,
                timestamp=datetime(2010, 1, 1, tzinfo=timezone.utc),
                voltage=-1.0,
                pump="alt_media",
                durations=[0, 1],
                volumes=[0, 1.5],
            )
        )
        cache["waste"] = encode(
            structs.WastePumpCalibration(
                name="setup_function",
                duration_=1.0,
                bias_=0,
                dc=60,
                hz=100,
                timestamp=datetime(2010, 1, 1, tzinfo=timezone.utc),
                voltage=-1.0,
                pump="waste",
                durations=[0, 1],
                volumes=[0, 1.5],
            )
        )


def test_silent_automation() -> None:
    experiment = "test_silent_automation"
    with Silent(volume=None, duration=60, unit=unit, experiment=experiment) as algo:
        pause()
        pubsub.publish(
            f"pioreactor/{unit}/{experiment}/od_reading/ods",
            encode(
                structs.ODReadings(
                    timestamp=current_utc_datetime(),
                    ods={
                        "2": structs.ODReading(
                            timestamp=current_utc_datetime(), angle="45", od=0.05, channel="2"
                        )
                    },
                )
            ),
        )
        pubsub.publish(
            f"pioreactor/{unit}/{experiment}/growth_rate_calculating/growth_rate",
            encode(structs.GrowthRate(growth_rate=0.01, timestamp=current_utc_datetime())),
        )
        pubsub.publish(
            f"pioreactor/{unit}/{experiment}/growth_rate_calculating/od_filtered",
            encode(structs.ODFiltered(od_filtered=1.0, timestamp=current_utc_datetime())),
        )
        pause()
        assert isinstance(algo.run(), events.NoEvent)
        assert algo.latest_normalized_od == 1.0
        assert algo.latest_growth_rate == 0.01
        assert algo.latest_od == {"2": 0.05}

        pubsub.publish(
            f"pioreactor/{unit}/{experiment}/od_reading/ods",
            encode(
                structs.ODReadings(
                    timestamp=current_utc_datetime(),
                    ods={
                        "2": structs.ODReading(
                            timestamp=current_utc_datetime(), angle="45", od=0.06, channel="2"
                        )
                    },
                )
            ),
        )
        pubsub.publish(
            f"pioreactor/{unit}/{experiment}/growth_rate_calculating/growth_rate",
            encode(structs.GrowthRate(growth_rate=0.02, timestamp=current_utc_datetime())),
        )
        pubsub.publish(
            f"pioreactor/{unit}/{experiment}/growth_rate_calculating/od_filtered",
            encode(structs.ODFiltered(od_filtered=1.1, timestamp=current_utc_datetime())),
        )
        pause()
        assert isinstance(algo.run(), events.NoEvent)
        assert algo.latest_normalized_od == 1.1
        assert algo.previous_normalized_od == 1.0

        assert algo.latest_growth_rate == 0.02
        assert algo.previous_growth_rate == 0.01

        assert algo.latest_od == {"2": 0.06}
        assert algo.previous_od == {"2": 0.05}


def test_turbidostat_automation() -> None:
    experiment = "test_turbidostat_automation"
    target_od = 1.0
    with Turbidostat(
        target_normalized_od=target_od, duration=60, volume=0.25, unit=unit, experiment=experiment
    ) as algo:

        pubsub.publish(
            f"pioreactor/{unit}/{experiment}/growth_rate_calculating/growth_rate",
            encode(structs.GrowthRate(growth_rate=0.01, timestamp=current_utc_datetime())),
        )
        pubsub.publish(
            f"pioreactor/{unit}/{experiment}/growth_rate_calculating/od_filtered",
            encode(structs.ODFiltered(od_filtered=0.98, timestamp=current_utc_datetime())),
        )
        pause()

        pubsub.publish(
            f"pioreactor/{unit}/{experiment}/growth_rate_calculating/growth_rate",
            encode(structs.GrowthRate(growth_rate=0.01, timestamp=current_utc_datetime())),
        )
        pubsub.publish(
            f"pioreactor/{unit}/{experiment}/growth_rate_calculating/od_filtered",
            encode(structs.ODFiltered(od_filtered=1.0, timestamp=current_utc_datetime())),
        )
        pause()
        assert isinstance(algo.run(), events.DilutionEvent)

        pubsub.publish(
            f"pioreactor/{unit}/{experiment}/growth_rate_calculating/growth_rate",
            encode(structs.GrowthRate(growth_rate=0.01, timestamp=current_utc_datetime())),
        )
        pubsub.publish(
            f"pioreactor/{unit}/{experiment}/growth_rate_calculating/od_filtered",
            encode(structs.ODFiltered(od_filtered=1.01, timestamp=current_utc_datetime())),
        )
        pause()
        assert isinstance(algo.run(), events.DilutionEvent)

        pubsub.publish(
            f"pioreactor/{unit}/{experiment}/growth_rate_calculating/growth_rate",
            encode(structs.GrowthRate(growth_rate=0.01, timestamp=current_utc_datetime())),
        )
        pubsub.publish(
            f"pioreactor/{unit}/{experiment}/growth_rate_calculating/od_filtered",
            encode(structs.ODFiltered(od_filtered=0.99, timestamp=current_utc_datetime())),
        )
        pause()
        assert algo.run() is None


def test_morbidostat_automation() -> None:
    experiment = "test_morbidostat_automation"
    pubsub.publish(
        f"pioreactor/{unit}/{experiment}/growth_rate_calculating/growth_rate",
        None,
        retain=True,
    )
    pubsub.publish(
        f"pioreactor/{unit}/{experiment}/growth_rate_calculating/od_filtered",
        None,
        retain=True,
    )

    target_od = 1.0
    algo = Morbidostat(
        target_normalized_od=target_od, duration=60, volume=0.25, unit=unit, experiment=experiment
    )

    pubsub.publish(
        f"pioreactor/{unit}/{experiment}/growth_rate_calculating/growth_rate",
        encode(structs.GrowthRate(growth_rate=0.01, timestamp=current_utc_datetime())),
    )
    pubsub.publish(
        f"pioreactor/{unit}/{experiment}/growth_rate_calculating/od_filtered",
        encode(structs.ODFiltered(od_filtered=0.95, timestamp=current_utc_datetime())),
    )
    pause()
    assert isinstance(algo.run(), events.NoEvent)

    pubsub.publish(
        f"pioreactor/{unit}/{experiment}/growth_rate_calculating/growth_rate",
        encode(structs.GrowthRate(growth_rate=0.01, timestamp=current_utc_datetime())),
    )
    pubsub.publish(
        f"pioreactor/{unit}/{experiment}/growth_rate_calculating/od_filtered",
        encode(structs.ODFiltered(od_filtered=0.99, timestamp=current_utc_datetime())),
    )
    pause()
    assert isinstance(algo.run(), events.DilutionEvent)

    pubsub.publish(
        f"pioreactor/{unit}/{experiment}/growth_rate_calculating/growth_rate",
        encode(structs.GrowthRate(growth_rate=0.01, timestamp=current_utc_datetime())),
    )
    pubsub.publish(
        f"pioreactor/{unit}/{experiment}/growth_rate_calculating/od_filtered",
        encode(structs.ODFiltered(od_filtered=1.05, timestamp=current_utc_datetime())),
    )
    pause()
    assert isinstance(algo.run(), events.AddAltMediaEvent)

    pubsub.publish(
        f"pioreactor/{unit}/{experiment}/growth_rate_calculating/growth_rate",
        encode(structs.GrowthRate(growth_rate=0.01, timestamp=current_utc_datetime())),
    )
    pubsub.publish(
        f"pioreactor/{unit}/{experiment}/growth_rate_calculating/od_filtered",
        encode(structs.ODFiltered(od_filtered=1.03, timestamp=current_utc_datetime())),
    )
    pause()
    assert isinstance(algo.run(), events.DilutionEvent)

    pubsub.publish(
        f"pioreactor/{unit}/{experiment}/growth_rate_calculating/growth_rate",
        encode(structs.GrowthRate(growth_rate=0.01, timestamp=current_utc_datetime())),
    )
    pubsub.publish(
        f"pioreactor/{unit}/{experiment}/growth_rate_calculating/od_filtered",
        encode(structs.ODFiltered(od_filtered=1.04, timestamp=current_utc_datetime())),
    )
    pause()
    assert isinstance(algo.run(), events.AddAltMediaEvent)

    pubsub.publish(
        f"pioreactor/{unit}/{experiment}/growth_rate_calculating/growth_rate",
        encode(structs.GrowthRate(growth_rate=0.01, timestamp=current_utc_datetime())),
    )
    pubsub.publish(
        f"pioreactor/{unit}/{experiment}/growth_rate_calculating/od_filtered",
        encode(structs.ODFiltered(od_filtered=0.99, timestamp=current_utc_datetime())),
    )
    pause()
    assert isinstance(algo.run(), events.DilutionEvent)
    algo.clean_up()


def test_pid_morbidostat_automation() -> None:
    experiment = "test_pid_morbidostat_automation"
    target_growth_rate = 0.09
    algo = PIDMorbidostat(
        target_od=1.0,
        target_growth_rate=target_growth_rate,
        duration=60,
        unit=unit,
        experiment=experiment,
    )

    pubsub.publish(
        f"pioreactor/{unit}/{experiment}/growth_rate_calculating/growth_rate",
        encode(structs.GrowthRate(growth_rate=0.08, timestamp=current_utc_datetime())),
    )
    pubsub.publish(
        f"pioreactor/{unit}/{experiment}/growth_rate_calculating/od_filtered",
        encode(structs.ODFiltered(od_filtered=0.5, timestamp=current_utc_datetime())),
    )
    pause()
    assert isinstance(algo.run(), events.NoEvent)
    pubsub.publish(
        f"pioreactor/{unit}/{experiment}/growth_rate_calculating/growth_rate",
        encode(structs.GrowthRate(growth_rate=0.08, timestamp=current_utc_datetime())),
    )
    pubsub.publish(
        f"pioreactor/{unit}/{experiment}/growth_rate_calculating/od_filtered",
        encode(structs.ODFiltered(od_filtered=0.95, timestamp=current_utc_datetime())),
    )
    pause()
    assert isinstance(algo.run(), events.AddAltMediaEvent)
    pubsub.publish(
        f"pioreactor/{unit}/{experiment}/growth_rate_calculating/growth_rate",
        encode(structs.GrowthRate(growth_rate=0.07, timestamp=current_utc_datetime())),
    )
    pubsub.publish(
        f"pioreactor/{unit}/{experiment}/growth_rate_calculating/od_filtered",
        encode(structs.ODFiltered(od_filtered=0.95, timestamp=current_utc_datetime())),
    )
    pause()
    assert isinstance(algo.run(), events.AddAltMediaEvent)
    pubsub.publish(
        f"pioreactor/{unit}/{experiment}/growth_rate_calculating/growth_rate",
        encode(structs.GrowthRate(growth_rate=0.065, timestamp=current_utc_datetime())),
    )
    pubsub.publish(
        f"pioreactor/{unit}/{experiment}/growth_rate_calculating/od_filtered",
        encode(structs.ODFiltered(od_filtered=0.95, timestamp=current_utc_datetime())),
    )
    pause()
    assert isinstance(algo.run(), events.AddAltMediaEvent)
    algo.clean_up()


def test_changing_morbidostat_parameters_over_mqtt() -> None:
    experiment = "test_changing_morbidostat_parameters_over_mqtt"
    target_growth_rate = 0.05
    algo = PIDMorbidostat(
        target_growth_rate=target_growth_rate,
        target_od=1.0,
        duration=60,
        unit=unit,
        experiment=experiment,
    )
    assert algo.target_growth_rate == target_growth_rate
    pause()
    new_target = 0.07
    pubsub.publish(
        f"pioreactor/{unit}/{experiment}/dosing_automation/target_growth_rate/set",
        new_target,
    )
    pause()
    assert algo.target_growth_rate == new_target
    assert algo.pid.pid.setpoint == new_target
    algo.clean_up()


def test_changing_turbidostat_params_over_mqtt() -> None:
    experiment = "test_changing_turbidostat_params_over_mqtt"
    og_volume = 0.5
    og_target_od = 1.0
    algo = Turbidostat(
        volume=og_volume,
        target_normalized_od=og_target_od,
        duration=60,
        unit=unit,
        experiment=experiment,
    )
    assert algo.volume == og_volume

    pubsub.publish(
        f"pioreactor/{unit}/{experiment}/growth_rate_calculating/growth_rate",
        encode(structs.GrowthRate(growth_rate=0.05, timestamp=current_utc_datetime())),
    )
    pubsub.publish(
        f"pioreactor/{unit}/{experiment}/growth_rate_calculating/od_filtered",
        encode(structs.ODFiltered(od_filtered=1.0, timestamp=current_utc_datetime())),
    )
    pause()
    algo.run()

    pubsub.publish(f"pioreactor/{unit}/{experiment}/dosing_automation/volume/set", 1.0)
    pause()

    pubsub.publish(
        f"pioreactor/{unit}/{experiment}/growth_rate_calculating/growth_rate",
        encode(structs.GrowthRate(growth_rate=0.05, timestamp=current_utc_datetime())),
    )
    pubsub.publish(
        f"pioreactor/{unit}/{experiment}/growth_rate_calculating/od_filtered",
        encode(structs.ODFiltered(od_filtered=1.0, timestamp=current_utc_datetime())),
    )
    algo.run()

    assert algo.volume == 1.0

    new_od = 1.5
    pubsub.publish(
        f"pioreactor/{unit}/{experiment}/dosing_automation/target_normalized_od/set", new_od
    )
    pause()
    assert algo.target_normalized_od == new_od
    algo.clean_up()


def test_changing_parameters_over_mqtt_with_unknown_parameter() -> None:
    experiment = "test_changing_parameters_over_mqtt_with_unknown_parameter"
    with pubsub.collect_all_logs_of_level("DEBUG", unit, experiment) as bucket:
        with DosingAutomationJob(
            target_growth_rate=0.05,
            target_od=1.0,
            duration=60,
            unit=unit,
            experiment=experiment,
        ):

            pubsub.publish(f"pioreactor/{unit}/{experiment}/dosing_automation/garbage/set", 0.07)
            # there should be a log published with "Unable to set garbage in dosing_automation"
            pause()
            pause()
            pause()

    assert len(bucket) > 0
    assert any(["garbage" in log["message"] for log in bucket])


def test_pause_in_dosing_automation() -> None:
    experiment = "test_pause_in_dosing_automation"
    with DosingAutomationJob(
        target_growth_rate=0.05,
        target_od=1.0,
        duration=60,
        unit=unit,
        experiment=experiment,
    ) as algo:
        pause()
        pubsub.publish(f"pioreactor/{unit}/{experiment}/dosing_automation/$state/set", "sleeping")
        pause()
        assert algo.state == "sleeping"

        pubsub.publish(f"pioreactor/{unit}/{experiment}/dosing_automation/$state/set", "ready")
        pause()
        assert algo.state == "ready"


def test_pause_in_dosing_control_also_pauses_automation() -> None:
    experiment = "test_pause_in_dosing_control_also_pauses_automation"
    algo = DosingController(
        "turbidostat",
        target_normalized_od=1.0,
        duration=5 / 60,
        volume=1.0,
        unit=unit,
        experiment=experiment,
    )
    pause()
    pubsub.publish(f"pioreactor/{unit}/{experiment}/dosing_control/$state/set", "sleeping")
    pause()
    assert algo.state == "sleeping"
    assert algo.automation_job.state == "sleeping"

    pubsub.publish(f"pioreactor/{unit}/{experiment}/dosing_control/$state/set", "ready")
    pause()
    assert algo.state == "ready"
    assert algo.automation_job.state == "ready"
    algo.clean_up()


def test_old_readings_will_not_execute_io() -> None:
    experiment = "test_old_readings_will_not_execute_io"
    with DosingAutomationJob(
        target_growth_rate=0.05,
        target_od=1.0,
        duration=60,
        unit=unit,
        experiment=experiment,
    ) as algo:
        algo._latest_growth_rate = 1
        algo._latest_normalized_od = 1

        algo.latest_normalized_od_at = current_utc_datetime() - timedelta(minutes=10)
        algo.latest_growth_rate_at = current_utc_datetime() - timedelta(minutes=4)

        assert algo.most_stale_time == algo.latest_normalized_od_at

        assert isinstance(algo.run(), events.NoEvent)


def test_throughput_calculator() -> None:
    experiment = "test_throughput_calculator"
    with local_persistant_storage("media_throughput") as c:
        c[experiment] = 0.0

    with local_persistant_storage("alt_media_throughput") as c:
        c[experiment] = 0.0

    with local_persistant_storage("alt_media_fraction") as c:
        c[experiment] = 0.0

    algo = DosingController(
        "pid_morbidostat",
        target_growth_rate=0.05,
        target_od=1.0,
        duration=60,
        unit=unit,
        experiment=experiment,
    )
    assert algo.automation_job.media_throughput == 0
    pause()
    pubsub.publish(
        f"pioreactor/{unit}/{experiment}/growth_rate_calculating/growth_rate",
        encode(structs.GrowthRate(growth_rate=0.08, timestamp=current_utc_datetime())),
    )
    pubsub.publish(
        f"pioreactor/{unit}/{experiment}/growth_rate_calculating/od_filtered",
        encode(structs.ODFiltered(od_filtered=1.0, timestamp=current_utc_datetime())),
    )
    pause()
    algo.automation_job.run()

    pubsub.publish(
        f"pioreactor/{unit}/{experiment}/growth_rate_calculating/growth_rate",
        encode(structs.GrowthRate(growth_rate=0.08, timestamp=current_utc_datetime())),
    )
    pubsub.publish(
        f"pioreactor/{unit}/{experiment}/growth_rate_calculating/od_filtered",
        encode(structs.ODFiltered(od_filtered=0.95, timestamp=current_utc_datetime())),
    )
    pause()
    algo.automation_job.run()
    assert algo.automation_job.media_throughput > 0
    assert algo.automation_job.alt_media_throughput > 0

    pubsub.publish(
        f"pioreactor/{unit}/{experiment}/growth_rate_calculating/growth_rate",
        encode(structs.GrowthRate(growth_rate=0.07, timestamp=current_utc_datetime())),
    )
    pubsub.publish(
        f"pioreactor/{unit}/{experiment}/growth_rate_calculating/od_filtered",
        encode(structs.ODFiltered(od_filtered=0.95, timestamp=current_utc_datetime())),
    )
    pause()
    algo.automation_job.run()
    assert algo.automation_job.media_throughput > 0
    assert algo.automation_job.alt_media_throughput > 0

    pubsub.publish(
        f"pioreactor/{unit}/{experiment}/growth_rate_calculating/growth_rate",
        encode(structs.GrowthRate(growth_rate=0.065, timestamp=current_utc_datetime())),
    )
    pubsub.publish(
        f"pioreactor/{unit}/{experiment}/growth_rate_calculating/od_filtered",
        encode(structs.ODFiltered(od_filtered=0.95, timestamp=current_utc_datetime())),
    )
    pause()
    algo.automation_job.run()
    assert algo.automation_job.media_throughput > 0
    assert algo.automation_job.alt_media_throughput > 0
    algo.clean_up()


def test_throughput_calculator_restart() -> None:
    experiment = "test_throughput_calculator_restart"
    with local_persistant_storage("media_throughput") as c:
        c[experiment] = 1.0

    with local_persistant_storage("alt_media_throughput") as c:
        c[experiment] = 1.5

    with DosingController(
        "turbidostat",
        target_normalized_od=1.0,
        duration=5 / 60,
        volume=1.0,
        unit=unit,
        experiment=experiment,
    ) as algo:
        pause()
        assert algo.automation_job.media_throughput == 1.0
        assert algo.automation_job.alt_media_throughput == 1.5


def test_throughput_calculator_manual_set() -> None:
    experiment = "test_throughput_calculator_manual_set"
    with local_persistant_storage("media_throughput") as c:
        c[experiment] = 1.0

    with local_persistant_storage("alt_media_throughput") as c:
        c[experiment] = 1.5

    with DosingController(
        "turbidostat",
        target_normalized_od=1.0,
        duration=5 / 60,
        volume=1.0,
        unit=unit,
        experiment=experiment,
    ) as algo:

        pause()
        assert algo.automation_job.media_throughput == 1.0
        assert algo.automation_job.alt_media_throughput == 1.5

        pubsub.publish(
            f"pioreactor/{unit}/{experiment}/dosing_automation/alt_media_throughput/set",
            0,
        )
        pubsub.publish(f"pioreactor/{unit}/{experiment}/dosing_automation/media_throughput/set", 0)
        pause()
        pause()
        assert algo.automation_job.media_throughput == 0
        assert algo.automation_job.alt_media_throughput == 0


def test_execute_io_action() -> None:
    experiment = "test_execute_io_action"
    with local_persistant_storage("media_throughput") as c:
        c[experiment] = 0.0

    with local_persistant_storage("alt_media_throughput") as c:
        c[experiment] = 0.0

    with DosingController("silent", unit=unit, experiment=experiment) as ca:
        ca.automation_job.execute_io_action(media_ml=0.65, alt_media_ml=0.35, waste_ml=0.65 + 0.35)
        pause()
        assert ca.automation_job.media_throughput == 0.65
        assert ca.automation_job.alt_media_throughput == 0.35

        ca.automation_job.execute_io_action(media_ml=0.15, alt_media_ml=0.15, waste_ml=0.3)
        pause()
        assert ca.automation_job.media_throughput == 0.80
        assert ca.automation_job.alt_media_throughput == 0.50

        ca.automation_job.execute_io_action(media_ml=1.0, alt_media_ml=0, waste_ml=1)
        pause()
        assert ca.automation_job.media_throughput == 1.80
        assert ca.automation_job.alt_media_throughput == 0.50

        ca.automation_job.execute_io_action(media_ml=0.0, alt_media_ml=1.0, waste_ml=1)
        pause()
        assert ca.automation_job.media_throughput == 1.80
        assert ca.automation_job.alt_media_throughput == 1.50


def test_execute_io_action2() -> None:
    experiment = "test_execute_io_action2"
    with local_persistant_storage("media_throughput") as c:
        c[experiment] = 0.0

    with local_persistant_storage("alt_media_throughput") as c:
        c[experiment] = 0.0

    with local_persistant_storage("alt_media_fraction") as c:
        c[experiment] = 0.0

    with DosingController("silent", unit=unit, experiment=experiment) as ca:
        ca.automation_job.execute_io_action(media_ml=1.25, alt_media_ml=0.01, waste_ml=1.26)
        pause()
        assert ca.automation_job.media_throughput == 1.25
        assert ca.automation_job.alt_media_throughput == 0.01
        assert abs(ca.automation_job.alt_media_fraction - 0.0007142) < 1e-5


def test_execute_io_action_outputs1() -> None:
    # regression test
    experiment = "test_execute_io_action_outputs1"
    with local_persistant_storage("media_throughput") as c:
        c[experiment] = 0.0

    with local_persistant_storage("alt_media_throughput") as c:
        c[experiment] = 0.0

    with local_persistant_storage("alt_media_fraction") as c:
        c[experiment] = 0.0

    with DosingAutomationJob(unit=unit, experiment=experiment) as ca:
        result = ca.execute_io_action(media_ml=1.25, alt_media_ml=0.01, waste_ml=1.26)
        assert result[0] == 1.25
        assert result[1] == 0.01
        assert result[2] == 1.26


def test_mqtt_properties_in_dosing_automations():
    experiment = "test_mqtt_properties"
    with local_persistant_storage("media_throughput") as c:
        c[experiment] = 0.0

    with local_persistant_storage("alt_media_throughput") as c:
        c[experiment] = 0.0

    with local_persistant_storage("alt_media_fraction") as c:
        c[experiment] = 0.0

    with DosingAutomationJob(unit=unit, experiment=experiment) as ca:
        r = pubsub.subscribe(
            f"pioreactor/{unit}/{experiment}/dosing_automation/alt_media_throughput"
        ).payload
        assert float(r) == 0

        r = pubsub.subscribe(
            f"pioreactor/{unit}/{experiment}/dosing_automation/media_throughput"
        ).payload
        assert float(r) == 0

        r = pubsub.subscribe(
            f"pioreactor/{unit}/{experiment}/dosing_automation/alt_media_fraction"
        ).payload
        assert float(r) == 0

        ca.execute_io_action(media_ml=0.35, alt_media_ml=0.25, waste_ml=0.6)

        r = pubsub.subscribe(
            f"pioreactor/{unit}/{experiment}/dosing_automation/alt_media_throughput"
        ).payload
        assert float(r) == 0.25

        r = pubsub.subscribe(
            f"pioreactor/{unit}/{experiment}/dosing_automation/media_throughput"
        ).payload
        assert float(r) == 0.35

        r = pubsub.subscribe(
            f"pioreactor/{unit}/{experiment}/dosing_automation/alt_media_fraction"
        ).payload
        assert abs(float(r) - 0.017857142) < 1e-6


def test_execute_io_action_outputs_will_be_null_if_calibration_is_not_defined() -> None:
    # regression test
    experiment = "test_execute_io_action_outputs_will_be_null_if_calibration_is_not_defined"

    with local_persistant_storage("current_pump_calibration") as cache:
        del cache["media"]
        del cache["alt_media"]

    with pytest.raises(exc.CalibrationError):
        with DosingAutomationJob(unit=unit, experiment=experiment, skip_first_run=True) as ca:
            ca.execute_io_action(media_ml=0.1, alt_media_ml=0.1, waste_ml=0.2)


def test_execute_io_action_outputs_will_shortcut_if_disconnected() -> None:
    # regression test
    experiment = "test_execute_io_action_outputs_will_shortcut_if_disconnected"
    with local_persistant_storage("media_throughput") as c:
        c[experiment] = 0.0

    with local_persistant_storage("alt_media_throughput") as c:
        c[experiment] = 0.0

    with local_persistant_storage("alt_media_fraction") as c:
        c[experiment] = 0.0

    ca = DosingAutomationJob(unit=unit, experiment=experiment)
    ca.clean_up()
    result = ca.execute_io_action(media_ml=1.25, alt_media_ml=0.01, waste_ml=1.26)
    assert result[0] == 0.0
    assert result[1] == 0.0
    assert result[2] == 0.0


def test_PIDMorbidostat() -> None:
    experiment = "test_PIDMorbidostat"
    algo = PIDMorbidostat(
        target_od=1.0,
        target_growth_rate=0.01,
        duration=5 / 60,
        unit=unit,
        experiment=experiment,
    )
    assert algo.latest_event is None
    pubsub.publish(
        f"pioreactor/{unit}/{experiment}/growth_rate_calculating/growth_rate",
        encode(structs.GrowthRate(growth_rate=0.08, timestamp=current_utc_datetime())),
    )
    pubsub.publish(
        f"pioreactor/{unit}/{experiment}/growth_rate_calculating/od_filtered",
        encode(structs.ODFiltered(od_filtered=0.5, timestamp=current_utc_datetime())),
    )
    time.sleep(10)
    pause()
    assert isinstance(algo.latest_event, events.NoEvent)

    pubsub.publish(
        f"pioreactor/{unit}/{experiment}/growth_rate_calculating/growth_rate",
        encode(structs.GrowthRate(growth_rate=0.08, timestamp=current_utc_datetime())),
    )
    pubsub.publish(
        f"pioreactor/{unit}/{experiment}/growth_rate_calculating/od_filtered",
        encode(structs.ODFiltered(od_filtered=0.95, timestamp=current_utc_datetime())),
    )
    time.sleep(20)
    pause()
    assert isinstance(algo.latest_event, events.AddAltMediaEvent)
    algo.clean_up()


def test_changing_duration_over_mqtt() -> None:
    experiment = "test_changing_duration_over_mqtt"
    with PIDMorbidostat(
        target_od=1.0,
        target_growth_rate=0.01,
        duration=5 / 60,
        unit=unit,
        experiment=experiment,
    ) as algo:
        assert algo.latest_event is None
        pubsub.publish(
            f"pioreactor/{unit}/{experiment}/growth_rate_calculating/growth_rate",
            encode(structs.GrowthRate(growth_rate=0.08, timestamp=current_utc_datetime())),
        )
        pubsub.publish(
            f"pioreactor/{unit}/{experiment}/growth_rate_calculating/od_filtered",
            encode(structs.ODFiltered(od_filtered=0.5, timestamp=current_utc_datetime())),
        )
        time.sleep(10)

        assert isinstance(algo.latest_event, events.NoEvent)

        pubsub.publish(
            f"pioreactor/{unit}/{experiment}/dosing_automation/duration/set",
            1,  # in minutes
        )
        time.sleep(10)
        assert algo.run_thread.interval == 60  # in seconds


def test_changing_duration_over_mqtt_will_start_next_run_earlier() -> None:
    experiment = "test_changing_duration_over_mqtt_will_start_next_run_earlier"
    with PIDMorbidostat(
        target_od=1.0,
        target_growth_rate=0.01,
        duration=10 / 60,
        unit=unit,
        experiment=experiment,
    ) as algo:
        assert algo.latest_event is None
        pubsub.publish(
            f"pioreactor/{unit}/{experiment}/growth_rate_calculating/growth_rate",
            encode(structs.GrowthRate(growth_rate=0.08, timestamp=current_utc_datetime())),
        )
        pubsub.publish(
            f"pioreactor/{unit}/{experiment}/growth_rate_calculating/od_filtered",
            encode(structs.ODFiltered(od_filtered=0.5, timestamp=current_utc_datetime())),
        )
        time.sleep(15)

        assert isinstance(algo.latest_event, events.NoEvent)

        pubsub.publish(
            f"pioreactor/{unit}/{experiment}/dosing_automation/duration/set",
            15 / 60,  # in minutes
        )
        time.sleep(5)
        assert algo.run_thread.interval == 15  # in seconds
        assert algo.run_thread.run_after > 0


def test_changing_algo_over_mqtt_with_wrong_automation_type() -> None:
    experiment = "test_changing_algo_over_mqtt_with_wrong_automation_type"
    with DosingController(
        "turbidostat",
        target_normalized_od=1.0,
        duration=5 / 60,
        volume=1.0,
        unit=unit,
        experiment=experiment,
    ) as algo:
        assert algo.automation.automation_name == "turbidostat"
        assert isinstance(algo.automation_job, Turbidostat)
        pubsub.publish(
            f"pioreactor/{unit}/{experiment}/dosing_control/automation/set",
            json.dumps(
                {
                    "automation_name": "pid_morbidostat",
                    "type": "led",
                    "args": {
                        "duration": 60,
                        "target_od": 1.0,
                        "target_growth_rate": 0.07,
                    },
                }
            ),
        )
        time.sleep(8)
        assert algo.automation.automation_name == "turbidostat"


def test_changing_algo_over_mqtt_solo() -> None:
    experiment = "test_changing_algo_over_mqtt_solo"
    with DosingController(
        "turbidostat",
        target_normalized_od=1.0,
        duration=5 / 60,
        volume=1.0,
        unit=unit,
        experiment=experiment,
    ) as algo:
        assert algo.automation.automation_name == "turbidostat"
        assert isinstance(algo.automation_job, Turbidostat)
        pubsub.publish(
            f"pioreactor/{unit}/{experiment}/dosing_control/automation/set",
            json.dumps(
                {
                    "automation_name": "pid_morbidostat",
                    "type": "dosing",
                    "args": {
                        "duration": 60,
                        "target_od": 1.0,
                        "target_growth_rate": 0.07,
                    },
                }
            ),
        )
        time.sleep(8)
        assert algo.automation.automation_name == "pid_morbidostat"
        assert isinstance(algo.automation_job, PIDMorbidostat)
        assert algo.automation_job.target_growth_rate == 0.07


@pytest.mark.skip(reason="this doesn't clean up properly")
def test_changing_algo_over_mqtt_when_it_fails_will_rollback() -> None:
    experiment = "test_changing_algo_over_mqtt_when_it_fails_will_rollback"
    with DosingController(
        "turbidostat",
        target_normalized_od=1.0,
        duration=5 / 60,
        volume=1.0,
        unit=unit,
        experiment=experiment,
    ) as algo:
        assert algo.automation.automation_name == "turbidostat"
        assert isinstance(algo.automation_job, Turbidostat)
        pause()
        pubsub.publish(
            f"pioreactor/{unit}/{experiment}/dosing_control/automation/set",
            json.dumps(
                {
                    "automation_name": "pid_morbidostat",
                    "args": {"duration": 60},
                    "type": "dosing",
                }
            ),
        )
        time.sleep(10)
        assert algo.automation.automation_name == "turbidostat"
        assert isinstance(algo.automation_job, Turbidostat)
        assert algo.automation_job.target_normalized_od == 1.0
        pause()
        pause()
        pause()


def test_changing_algo_over_mqtt_will_not_produce_two_dosing_jobs() -> None:
    experiment = "test_changing_algo_over_mqtt_will_not_produce_two_dosing_jobs"
    with local_persistant_storage("media_throughput") as c:
        c[experiment] = 0.0

    with local_persistant_storage("alt_media_throughput") as c:
        c[experiment] = 0.0

    with local_persistant_storage("alt_media_fraction") as c:
        c[experiment] = 0.0

    algo = DosingController(
        "turbidostat",
        volume=1.0,
        target_normalized_od=0.4,
        duration=60,
        unit=unit,
        experiment=experiment,
    )
    assert algo.automation.automation_name == "turbidostat"
    pause()
    pubsub.publish(
        f"pioreactor/{unit}/{experiment}/dosing_control/automation/set",
        json.dumps(
            {
                "automation_name": "turbidostat",
                "type": "dosing",
                "args": {
                    "duration": 60,
                    "target_normalized_od": 1.0,
                    "volume": 1.0,
                    "skip_first_run": 1,
                },
            }
        ),
    )
    time.sleep(10)  # need to wait for all jobs to disconnect correctly and threads to join.
    assert isinstance(algo.automation_job, Turbidostat)

    pubsub.publish(
        f"pioreactor/{unit}/{experiment}/growth_rate_calculating/growth_rate",
        encode(structs.GrowthRate(growth_rate=1.0, timestamp=current_utc_datetime())),
    )
    pubsub.publish(
        f"pioreactor/{unit}/{experiment}/growth_rate_calculating/od_filtered",
        encode(structs.ODFiltered(od_filtered=1.0, timestamp=current_utc_datetime())),
    )
    pause()

    # note that we manually run, as we have skipped the first run in the json
    algo.automation_job.run()
    time.sleep(5)
    assert algo.automation_job.media_throughput == 1.0

    pubsub.publish(
        f"pioreactor/{unit}/{experiment}/dosing_automation/target_normalized_od/set", 1.5
    )
    pause()
    pause()
    assert algo.automation_job.target_normalized_od == 1.5
    algo.clean_up()


def test_changing_algo_over_mqtt_with_wrong_type_is_okay() -> None:
    experiment = "test_changing_algo_over_mqtt_with_wrong_type_is_okay"
    with local_persistant_storage("media_throughput") as c:
        c[experiment] = 0.0

    algo = DosingController(
        "turbidostat",
        volume=1.0,
        target_normalized_od=0.4,
        duration=2 / 60,
        unit=unit,
        experiment=experiment,
    )
    assert algo.automation.automation_name == "turbidostat"
    assert algo.automation_name == "turbidostat"
    pause()
    pubsub.publish(
        f"pioreactor/{unit}/{experiment}/dosing_control/automation/set",
        json.dumps(
            {
                "automation_name": "turbidostat",
                "type": "dosing",
                "args": {"duration": "60", "target_normalized_od": "1.0", "volume": "1.0"},
            }
        ),
    )
    time.sleep(7)  # need to wait for all jobs to disconnect correctly and threads to join.
    assert isinstance(algo.automation_job, Turbidostat)
    assert algo.automation_job.target_normalized_od == 1.0
    algo.clean_up()


def test_disconnect_cleanly() -> None:
    experiment = "test_disconnect_cleanly"
    algo = DosingController(
        "turbidostat",
        target_normalized_od=1.0,
        duration=50,
        unit=unit,
        volume=1.0,
        experiment=experiment,
    )
    assert algo.automation.automation_name == "turbidostat"
    assert isinstance(algo.automation_job, Turbidostat)
    pubsub.publish(f"pioreactor/{unit}/{experiment}/dosing_control/$state/set", "disconnected")
    time.sleep(10)
    assert algo.state == algo.DISCONNECTED


def test_disconnect_cleanly_during_pumping_execution() -> None:
    experiment = "test_disconnect_cleanly_during_pumping_execution"
    algo = DosingController(
        "chemostat",
        volume=5.0,
        duration=10,
        unit=unit,
        experiment=experiment,
    )
    assert algo.automation.automation_name == "chemostat"
    time.sleep(4)
    pubsub.publish(f"pioreactor/{unit}/{experiment}/dosing_control/$state/set", "disconnected")
    time.sleep(10)
    assert algo.state == algo.DISCONNECTED
    assert algo.automation_job.state == algo.DISCONNECTED


def test_custom_class_will_register_and_run() -> None:
    experiment = "test_custom_class_will_register_and_run"

    class NaiveTurbidostat(DosingAutomationJob):

        automation_name = "naive_turbidostat"
        published_settings = {
            "target_od": {"datatype": "float", "settable": True, "unit": "AU"},
            "duration": {"datatype": "float", "settable": True, "unit": "min"},
        }

        def __init__(self, target_od: float, **kwargs: Any) -> None:
            super(NaiveTurbidostat, self).__init__(**kwargs)
            self.target_od = target_od

        def execute(self) -> None:
            if self.latest_normalized_od > self.target_od:
                self.execute_io_action(media_ml=1.0, waste_ml=1.0)

    with DosingController(
        "naive_turbidostat",
        target_od=2.0,
        duration=10,
        unit=get_unit_name(),
        experiment=experiment,
    ):
        pass


def test_what_happens_when_no_od_data_is_coming_in() -> None:
    experiment = "test_what_happens_when_no_od_data_is_coming_in"
    pubsub.publish(
        f"pioreactor/{unit}/{experiment}/growth_rate_calculating/growth_rate",
        None,
        retain=True,
    )
    pubsub.publish(
        f"pioreactor/{unit}/{experiment}/growth_rate_calculating/od_filtered",
        None,
        retain=True,
    )

    algo = Turbidostat(
        target_normalized_od=0.1, duration=40 / 60, volume=0.25, unit=unit, experiment=experiment
    )
    pause()
    event = algo.run()
    assert isinstance(event, events.ErrorOccurred)
    algo.clean_up()


def test_changing_duty_cycle_over_mqtt() -> None:
    experiment = "test_changing_duty_cycle_over_mqtt"
    with ContinuousCycle(unit=unit, experiment=experiment) as algo:

        assert algo.duty_cycle == 100
        pubsub.publish(f"pioreactor/{unit}/{experiment}/dosing_automation/duty_cycle/set", 50)
        pause()
        assert algo.duty_cycle == 50


def test_AltMediaCalculator() -> None:
    from pioreactor.structs import DosingEvent

    ac = AltMediaCalculator()
    vial_volume = ac.vial_volume

    media_added = 1.0
    add_media_event = DosingEvent(
        volume_change=media_added, event="add_media", timestamp="0", source_of_event="test"
    )
    assert ac.update(add_media_event, 0.0) == 0.0
    assert abs(ac.update(add_media_event, 0.20) - 0.20 * (1 - (media_added / vial_volume))) < 1e-10
    assert abs(ac.update(add_media_event, 1.0) - 1.0 * (1 - (media_added / vial_volume))) < 1e-10

    alt_media_added = 1.0
    add_alt_media_event = DosingEvent(
        volume_change=alt_media_added, event="add_alt_media", timestamp="0", source_of_event="test"
    )
    assert ac.update(add_alt_media_event, 0.0) == alt_media_added / vial_volume

    alt_media_added = 2.0
    add_alt_media_event = DosingEvent(
        volume_change=alt_media_added, event="add_alt_media", timestamp="0", source_of_event="test"
    )
    assert ac.update(add_alt_media_event, 0.0) == alt_media_added / vial_volume

    alt_media_added = 0.0001
    add_alt_media_event = DosingEvent(
        volume_change=alt_media_added, event="add_alt_media", timestamp="0", source_of_event="test"
    )
    assert ac.update(add_alt_media_event, 0.6) > 0.6


def test_latest_event_goes_to_mqtt():
    experiment = "test_latest_event_goes_to_mqtt"

    class FakeAutomation(DosingAutomationJob):
        """
        Do nothing, ever. Just pass.
        """

        automation_name = "fake_automation"
        published_settings = {"duration": {"datatype": "float", "settable": True, "unit": "min"}}

        def __init__(self, **kwargs) -> None:
            super(FakeAutomation, self).__init__(**kwargs)

        def execute(self):
            return events.NoEvent(message="demo", data={"d": 1.0, "s": "test"})

    with DosingController(
        "fake_automation",
        duration=0.1,
        unit=get_unit_name(),
        experiment=experiment,
    ) as dc:
        assert "latest_event" in dc.automation_job.published_settings

        msg = pubsub.subscribe(f"pioreactor/{unit}/{experiment}/dosing_automation/latest_event")
        assert msg is not None

        latest_event_from_mqtt = json.loads(msg.payload)
        assert latest_event_from_mqtt["event_name"] == "NoEvent"
        assert latest_event_from_mqtt["message"] == "demo"
        assert latest_event_from_mqtt["data"]["d"] == 1.0
        assert latest_event_from_mqtt["data"]["s"] == "test"


def test_strings_are_okay_for_chemostat():
    unit = get_unit_name()
    experiment = "test_strings_are_okay_for_chemostat"

    with local_persistant_storage("media_throughput") as c:
        c[experiment] = 0.0

    with local_persistant_storage("alt_media_throughput") as c:
        c[experiment] = 0.0

    with local_persistant_storage("alt_media_fraction") as c:
        c[experiment] = 0.0

    with start_dosing_control(
        "chemostat", "20", False, unit, experiment, volume="0.7"
    ) as controller:
        assert controller.automation_job.volume == 0.7
        pause(n=35)
        assert controller.automation_job.media_throughput == 0.7


def test_chemostat_from_cli():
    from pioreactor.cli.pio import pio

    t = Timer(
        15,
        pubsub.publish,
        args=(
            "pioreactor/testing_unit/_testing_experiment/dosing_control/$state/set",
            "disconnected",
        ),
    )
    t.start()

    with pubsub.collect_all_logs_of_level("ERROR", "testing_unit", "_testing_experiment") as errors:
        runner = CliRunner()
        result = runner.invoke(
            pio, ["run", "dosing_control", "--automation-name", "chemostat", "--volume", "1.5"]
        )

    assert result.exit_code == 0
    assert len(errors) == 0
