# -*- coding: utf-8 -*-
# test_od_reading.py
from __future__ import annotations

import time

import numpy as np
import pytest
from msgspec.json import encode

from pioreactor import exc
from pioreactor import structs
from pioreactor.background_jobs.od_reading import ADCReader
from pioreactor.background_jobs.od_reading import CachedCalibrationTransformer
from pioreactor.background_jobs.od_reading import NullCalibrationTransformer
from pioreactor.background_jobs.od_reading import ODReader
from pioreactor.background_jobs.od_reading import start_od_reading
from pioreactor.pubsub import collect_all_logs_of_level
from pioreactor.utils import local_persistant_storage
from pioreactor.utils.timing import current_utc_datetime
from pioreactor.whoami import get_unit_name


def pause(n=1) -> None:
    time.sleep(n * 0.25)


def test_sin_regression_exactly() -> None:

    freq = 60
    x = [i / 25 for i in range(25)]
    y = [10 + 2 * np.sin(freq * 2 * np.pi * _x) + 0.1 * np.random.randn() for _x in x]

    adc_reader = ADCReader(channels=[])

    (C, A, phi), _ = adc_reader.sin_regression_with_known_freq(x, y, freq)
    assert isinstance(A, float)
    assert isinstance(phi, float)
    assert np.abs(C - 10) < 0.1
    assert np.abs(A - 2) < 0.1
    assert np.abs(phi - 0) < 0.1

    freq = 50
    # interestingly, if I used i/25, I get a matrix inversion problem, likely because 25 | 50. This shows the importance of adding a small jitter.
    x = [(i / 25 + 0.001 * (i * 0.618034) % 1) for i in range(25)]
    y = [10 + 2 * np.sin(freq * 2 * np.pi * _x) + 0.1 * np.random.randn() for _x in x]

    adc_reader = ADCReader(channels=[])

    (C, A, phi), _ = adc_reader.sin_regression_with_known_freq(x, y, freq)
    assert isinstance(A, float)
    assert isinstance(phi, float)
    assert np.abs(C - 10) < 0.1
    assert np.abs(A - 2) < 0.1
    assert np.abs(phi - 0) < 0.1


def test_sin_regression_all_zeros_should_return_zeros() -> None:
    import numpy as np

    with np.errstate(all="raise"):
        adc_reader = ADCReader(channels=[])

        (C, A, phi), AIC = adc_reader.sin_regression_with_known_freq(
            [i / 25 for i in range(25)], [0] * 25, 60
        )
        assert C == 0
        assert A == 0
        assert np.isinf(AIC)


def test_sin_regression_real_data_and_that_60hz_is_the_minimum() -> None:

    y = [
        8694,
        8622,
        8587,
        8537,
        8533,
        8529,
        8556,
        8582,
        8698,
        8734,
        8841,
        8980,
        9005,
        9050,
        9077,
        9091,
        9107,
        9118,
        9102,
        9037,
        9006,
        8893,
        8855,
        8755,
        8597,
        8565,
    ]
    x = [
        6.849016062915325e-05,
        0.03225604514591396,
        0.06504625407978892,
        0.09745802800171077,
        0.13046979811042547,
        0.1631201640702784,
        0.19538412615656853,
        0.22827485506422818,
        0.2607731909956783,
        0.29389490908943117,
        0.3266107430681586,
        0.35897407913580537,
        0.39195163105614483,
        0.42453178903087974,
        0.45695877098478377,
        0.48978127096779644,
        0.5222139300312847,
        0.5552757519762963,
        0.5879572120029479,
        0.6202454441227019,
        0.6531873710919172,
        0.6857172690797597,
        0.7188976851757616,
        0.751680811168626,
        0.7840821680147201,
        0.8170840430539101,
    ]

    adc_reader = ADCReader(channels=[])
    (C, A, phi), AIC_60 = adc_reader.sin_regression_with_known_freq(x, y, 60)
    assert abs(C - np.mean(y)) < 10

    for i in range(2, 75):
        # skip i=32, noise
        if i == 32:
            continue

        (C, A, phi), AIC_i = adc_reader.sin_regression_with_known_freq(x, y, i)
        assert AIC_i >= AIC_60


def test_sin_regression_real_data_and_that_60hz_is_the_minimum2() -> None:

    y = [
        6393,
        6470,
        6523,
        6373,
        6375,
        6234,
        6147,
        6283,
        6264,
        6206,
        6276,
        6070,
        6047,
        6018,
        6162,
        6323,
        6044,
        5887,
        6100,
        6032,
        5988,
        6021,
        6043,
        6189,
        6299,
        6279,
    ]
    x = [
        0.011080539086833596,
        0.043024661019444466,
        0.07600112399086356,
        0.10825863108038902,
        0.14143993705511093,
        0.17404911015182734,
        0.2063259920105338,
        0.2390361011493951,
        0.2709076199680567,
        0.30479230894707143,
        0.337737939087674,
        0.3699464879464358,
        0.40293430513702333,
        0.4354570710565895,
        0.4678043171297759,
        0.5005605730693787,
        0.5329271419905126,
        0.5661279789637774,
        0.5986463711597025,
        0.6308952320832759,
        0.6638960181735456,
        0.6964425339829177,
        0.7295078509487212,
        0.7623454600106925,
        0.7947157791350037,
        0.8276583361439407,
    ]

    adc_reader = ADCReader(channels=[])
    (C, A, phi), AIC_60 = adc_reader.sin_regression_with_known_freq(x, y, 60)
    assert abs(C - np.mean(y)) < 10

    for i in range(2, 75):
        if i == 30 or i == 31 or i == 62:
            continue

        (C_, _, _), AIC_i = adc_reader.sin_regression_with_known_freq(x, y, i)

        assert AIC_i >= AIC_60, i


def test_sin_regression_constant_should_return_constant() -> None:

    adc_reader = ADCReader(channels=[])

    (C, A, phi), _ = adc_reader.sin_regression_with_known_freq(
        [i / 25 for i in range(25)], [1.0] * 25, 60
    )
    assert C == 1.0
    assert abs(A - 0.0) < 1e-10  # type: ignore


def test_sin_regression_with_linear_change_should_return_close_to_mean() -> None:

    adc_reader = ADCReader(channels=[])

    y = [i for i in range(25)]

    (C, A, phi), _ = adc_reader.sin_regression_with_known_freq([i / 25 for i in range(25)], y, 60)
    assert np.abs(C - np.mean(y)) < 0.001


def test_sin_regression_with_slightly_lower_frequency() -> None:
    # https://electronics.stackexchange.com/questions/57878/how-precise-is-the-frequency-of-the-ac-electricity-network
    actual_freq = 59.5

    x = [i / 25 for i in range(25)]
    y = [10 + np.sin(actual_freq * 2 * np.pi * _x) + 0.1 * np.random.randn() for _x in x]

    adc_reader = ADCReader(channels=[])

    (C, A, phi), _ = adc_reader.sin_regression_with_known_freq(x, y, 60)
    assert np.abs(C - 10) < 0.1


def test_sin_regression_with_slightly_higher_frequency_but_correct_freq_has_better_fit() -> None:
    # https://electronics.stackexchange.com/questions/57878/how-precise-is-the-frequency-of-the-ac-electricity-network
    actual_freq = 60.5

    x = [i / 25 for i in range(25)]
    y = [10 + np.sin(actual_freq * 2 * np.pi * _x) + 0.1 * np.random.randn() for _x in x]

    adc_reader = ADCReader(channels=[])

    (C_60, A, phi), aic_60 = adc_reader.sin_regression_with_known_freq(x, y, 60)
    assert np.abs(C_60 - 10) < 0.1

    (C_61, A, phi), aic_61 = adc_reader.sin_regression_with_known_freq(x, y, actual_freq)
    assert aic_61 < aic_60  # lower is better


def test_sin_regression_with_strong_penalizer() -> None:

    adc_reader = ADCReader(channels=[])

    (C, A, phi), _ = adc_reader.sin_regression_with_known_freq(
        [i / 25 for i in range(25)], [100] * 25, 60, prior_C=125, penalizer_C=1_000_000
    )
    assert abs(C - 125) < 0.01


def test_ADC_picks_to_correct_freq() -> None:

    actual_freq = 50.0

    x = [i / 25 + 0.005 * np.random.randn() for i in range(25)]
    y = [10 + np.sin(actual_freq * 2 * np.pi * _x) + 0.1 * np.random.randn() for _x in x]

    adc_reader = ADCReader(channels=["1"])

    best_freq = adc_reader.determine_most_appropriate_AC_hz({"1": x}, {"1": y})
    assert best_freq == actual_freq

    actual_freq = 60.0

    x = [i / 25 + 0.005 * np.random.randn() for i in range(25)]
    y = [2 + np.sin(actual_freq * 2 * np.pi * _x) + 0.1 * np.random.randn() for _x in x]

    adc_reader = ADCReader(channels=["1"])

    best_freq = adc_reader.determine_most_appropriate_AC_hz({"1": x}, {"1": y})
    assert best_freq == actual_freq


def test_ADC_picks_to_correct_freq_even_if_slight_noise_in_freq() -> None:

    actual_freq = 50.0

    x = [i / 25 + 0.005 * np.random.randn() for i in range(25)]
    y = [10 + np.sin((actual_freq + 0.2) * 2 * np.pi * _x) + 0.1 * np.random.randn() for _x in x]

    adc_reader = ADCReader(channels=["1"])

    best_freq = adc_reader.determine_most_appropriate_AC_hz({"1": x}, {"1": y})
    assert best_freq == actual_freq


def test_error_thrown_if_wrong_angle() -> None:
    with pytest.raises(ValueError):
        start_od_reading("100", "135", fake_data=True, experiment="test_error_thrown_if_wrong_angle")  # type: ignore

    with pytest.raises(ValueError):
        start_od_reading("100", None, fake_data=True, experiment="test_error_thrown_if_wrong_angle")  # type: ignore

    with pytest.raises(ValueError):
        start_od_reading("135", "99", fake_data=True, experiment="test_error_thrown_if_wrong_angle")  # type: ignore

    with pytest.raises(ValueError):
        start_od_reading("100", "REF", fake_data=True, experiment="test_error_thrown_if_wrong_angle")  # type: ignore

    st = start_od_reading(
        "135", "90", fake_data=True, experiment="test_error_thrown_if_wrong_angle"
    )
    st.clean_up()


def test_sin_regression_penalizer_C_is_independent_of_scale_of_observed_values() -> None:

    freq = 60
    C_True = 10
    x = [i / 25 for i in range(25)]
    y = [C_True + 2 * np.sin(freq * 2 * np.pi * _x) + 0.1 * np.random.randn() for _x in x]

    adc_reader = ADCReader(channels=[])

    (C, A, phi), _ = adc_reader.sin_regression_with_known_freq(
        x, y, freq, prior_C=12, penalizer_C=10.0
    )
    ratio = C / C_True

    # scale everything by 10
    factor = 10
    y = [factor * y_ for y_ in y]

    adc_reader = ADCReader(channels=[])

    (C, A, phi), _ = adc_reader.sin_regression_with_known_freq(
        x, y, freq, prior_C=factor * 12, penalizer_C=10.0
    )
    assert np.abs(C / (factor * C_True) - ratio) < 0.01


def test_sin_regression_all_negative() -> None:

    freq = 60
    x = [i / 25 for i in range(25)]
    y = [-2 for _x in x]

    adc_reader = ADCReader(channels=[])

    (C, A, phi), AIC = adc_reader.sin_regression_with_known_freq(x, y, freq)
    assert C == -2
    assert AIC == float("inf")


def test_simple_API() -> None:
    od_job = start_od_reading("90", "REF", interval=100_000, fake_data=True, use_calibration=False)

    for led_int in range(5, 70, 15):
        time.sleep(2)
        od_job.ir_led_intensity = led_int
        od_job.start_ir_led()
        assert od_job.ir_led_intensity == led_int
        results = od_job.record_from_adc()
        assert list(results.ods.keys()) == ["1"]

    od_job.clean_up()


def test_ability_to_be_iterated() -> None:
    od_stream = start_od_reading(
        "90",
        "REF",
        interval=1.0,
        fake_data=True,
        experiment="test_ability_to_be_iterated",
        use_calibration=False,
    )
    results = []

    for i, reading in enumerate(od_stream):
        results.append(reading)
        if i == 5:
            break

    assert len(results) > 0
    assert results[0].timestamp < results[1].timestamp < results[2].timestamp
    od_stream.clean_up()


def test_add_pre_read_callback() -> None:
    def cb(od_job):
        od_job.ir_led_intensity = 15

    ODReader.add_pre_read_callback(cb)

    od = start_od_reading("45", "REF", interval=1, fake_data=True, use_calibration=False)
    pause()
    pause()
    pause()
    pause()
    assert od.ir_led_intensity == 15
    od.clean_up()

    # clear it again.
    ODReader._pre_read.clear()


def test_add_post_read_callback() -> None:
    def cb(od_job, batched_readings, *args):
        od_job.logger.critical("is lunch ready?")

    ODReader.add_post_read_callback(cb)

    with collect_all_logs_of_level(
        "CRITICAL", experiment="test_add_post_read_callback", unit="test"
    ) as bucket:
        od = start_od_reading(
            "45",
            "REF",
            interval=1,
            fake_data=True,
            experiment="test_add_post_read_callback",
            unit="test",
            use_calibration=False,
        )
        pause(25)
        od.clean_up()
        assert len(bucket) > 0

    # clear it again.
    ODReader._post_read.clear()


def test_outliers_are_removed_in_sin_regression() -> None:

    freq = 60
    x = [
        6.973999552428722e-05,
        0.03355777799151838,
        0.06766039799549617,
        0.1013846330170054,
        0.13573287799954414,
        0.1696973209909629,
        0.2032879629987292,
        0.23748631199123338,
        0.27130481801577844,
        0.3057407700107433,
        0.33978755699354224,
        0.373446949000936,
        0.40773284900933504,
        0.4416320839955006,
        0.4753085080010351,
        0.5095541480113752,
        0.5434022890112828,
        0.5778828249895014,
        0.6119594550109468,
        0.6456623889971524,
        0.679938810004387,
        0.7139212219917681,
        0.7484785279957578,
        0.7826525020063855,
        0.8165176229958888,
    ]
    adc_reader = ADCReader(channels=[])

    y_with_outlier = [
        11321,
        249,
        180,
        123,
        160,
        125,
        59,
        96,
        105,
        177,
        213,
        184,
        237,
        264,
        304,
        325,
        295,
        307,
        295,
        396,
        336,
        252,
        207,
        118,
        100,
    ]
    (C1, A, phi), _ = adc_reader.sin_regression_with_known_freq(list(x), y_with_outlier, freq)

    y_without_outlier = [
        211,
        249,
        180,
        123,
        160,
        125,
        59,
        96,
        105,
        177,
        213,
        184,
        237,
        264,
        304,
        325,
        295,
        307,
        295,
        396,
        336,
        252,
        207,
        118,
        100,
    ]
    (C2, A, phi), _ = adc_reader.sin_regression_with_known_freq(list(x), y_without_outlier, freq)

    assert np.abs(C1 - C2) < 5


def test_calibration_not_requested():
    with start_od_reading("90", "REF", interval=None, fake_data=True, use_calibration=False) as od:
        assert isinstance(od.calibration_transformer, NullCalibrationTransformer)
        assert od.calibration_transformer({"2": 0.1}) == {"2": 0.1}
        assert od.calibration_transformer({"2": 0.5, "1": 0.0}) == {"2": 0.5, "1": 0.0}


def test_calibration_not_present():

    with local_persistant_storage("current_od_calibration") as c:
        if "90" in c:
            del c["90"]

    with start_od_reading("90", "REF", interval=None, fake_data=True, use_calibration=True) as od:
        assert isinstance(od.calibration_transformer, CachedCalibrationTransformer)
        assert len(od.calibration_transformer.models) == 0


def test_calibration_simple_linear_calibration():
    experiment = "test_calibration_simple_linear_calibration"

    with local_persistant_storage("current_od_calibration") as c:
        c["90"] = encode(
            structs.OD90Calibration(
                timestamp=current_utc_datetime(),
                curve_type="poly",
                curve_data_=[2.0, 0.0],
                name="linear",
                maximum_od600=2.0,
                minimum_od600=0.0,
                ir_led_intensity=90.0,
                angle="90",
                minimum_voltage=0.0,
                maximum_voltage=1.0,
                voltages=[],
                inferred_od600s=[],
                pd_channel="2",
            )
        )

    with start_od_reading(
        "REF",
        "90",
        interval=None,
        fake_data=True,
        experiment=experiment,
        unit=get_unit_name(),
        use_calibration=True,
    ) as od:
        assert isinstance(od.calibration_transformer, CachedCalibrationTransformer)

        voltage = 0.0
        assert od.calibration_transformer.models["2"](voltage) == (voltage - 0) / 2

        voltage = 0.5
        assert od.calibration_transformer.models["2"](voltage) == (voltage - 0) / 2

        with collect_all_logs_of_level("debug", unit=get_unit_name(), experiment="+") as bucket:
            voltage = 10.0
            pause()
            pause()
            pause()
            assert od.calibration_transformer.models["2"](voltage) == 2.0
            pause()
            pause()
            pause()
            assert "suggested" in bucket[0]["message"]

    with local_persistant_storage("current_od_calibration") as c:
        del c["90"]


def test_calibration_simple_linear_calibration_negative_slope():
    experiment = "test_calibration_simple_linear_calibration_negative_slope"

    with local_persistant_storage("current_od_calibration") as c:
        c["90"] = encode(
            structs.OD90Calibration(
                timestamp=current_utc_datetime(),
                curve_type="poly",
                curve_data_=[-0.1, 2],
                name="linear",
                maximum_od600=20.0,
                minimum_od600=0.0,
                ir_led_intensity=90.0,
                angle="90",
                minimum_voltage=0.0,
                maximum_voltage=2.0,
                voltages=[],
                inferred_od600s=[],
                pd_channel="2",
            )
        )

    with start_od_reading(
        "REF", "90", interval=None, fake_data=True, experiment=experiment, unit=get_unit_name()
    ) as od:
        assert isinstance(od.calibration_transformer, CachedCalibrationTransformer)

        voltage = 0.0
        assert od.calibration_transformer.models["2"](voltage) == (voltage - 2) / (-0.1)

        voltage = 0.5
        assert od.calibration_transformer.models["2"](voltage) == (voltage - 2) / (-0.1)

        with collect_all_logs_of_level("debug", unit=get_unit_name(), experiment="+") as bucket:
            voltage = 12.0
            assert voltage > 2.0

            pause()
            assert od.calibration_transformer.models["2"](voltage) == 20.0
            pause()
            pause()
            assert "suggested" in bucket[0]["message"]
    with local_persistant_storage("current_od_calibration") as c:
        del c["90"]


def test_calibration_simple_quadratic_calibration():
    experiment = "test_calibration_simple_quadratic_calibration"

    with local_persistant_storage("current_od_calibration") as c:
        c["90"] = encode(
            structs.OD90Calibration(
                timestamp=current_utc_datetime(),
                curve_type="poly",
                curve_data_=[1.0, 0, -0.1],
                name="quad_test",
                maximum_od600=2.0,
                minimum_od600=0.0,
                ir_led_intensity=90.0,
                angle="90",
                minimum_voltage=0.0,
                maximum_voltage=1.0,
                voltages=[],
                inferred_od600s=[],
                pd_channel="2",
            )
        )

    with start_od_reading(
        "REF", "90", interval=None, fake_data=True, experiment=experiment, unit=get_unit_name()
    ) as od:
        assert isinstance(od.calibration_transformer, CachedCalibrationTransformer)
        x = 0.5
        assert abs(od.calibration_transformer.models["2"](x) - np.sqrt(3 / 5)) < 0.001
    with local_persistant_storage("current_od_calibration") as c:
        del c["90"]


def test_calibration_multi_modal():
    experiment = "test_calibration_multi_modal"
    # note: not a realistic calibration curve, using only because it's unimodal
    poly = [0.2983, -0.585, 0.146, 0.261, 0.0]  # unimodal, peak near ~(0.74, 0.120)

    with local_persistant_storage("current_od_calibration") as c:
        c["90"] = encode(
            structs.OD90Calibration(
                timestamp=current_utc_datetime(),
                curve_type="poly",
                curve_data_=poly,
                name="multi_test",
                maximum_od600=2.0,
                minimum_od600=0.0,
                ir_led_intensity=90.0,
                angle="90",
                minimum_voltage=0.0,
                maximum_voltage=1.0,
                voltages=[],
                inferred_od600s=[],
                pd_channel="2",
            )
        )

    with start_od_reading("REF", "90", interval=None, fake_data=True, experiment=experiment) as od:
        assert isinstance(od.calibration_transformer, CachedCalibrationTransformer)
        for i in range(0, 1000):

            voltage = np.polyval(poly, i / 1000)
            print(voltage, od.calibration_transformer.models["2"](voltage))

    with local_persistant_storage("current_od_calibration") as c:
        del c["90"]


def test_calibration_errors_when_ir_led_differs():

    experiment = "test_calibration_errors_when_ir_led_differs"

    with local_persistant_storage("current_od_calibration") as c:
        c["90"] = encode(
            structs.OD90Calibration(
                timestamp=current_utc_datetime(),
                curve_type="poly",
                curve_data_=[1.0, 0, -0.1],
                name="quad_test",
                maximum_od600=2.0,
                minimum_od600=0.0,
                ir_led_intensity=50.0,
                angle="90",
                minimum_voltage=0.0,
                maximum_voltage=1.0,
                voltages=[],
                inferred_od600s=[],
                pd_channel="2",
            )
        )

    with pytest.raises(exc.CalibrationError) as error:
        with start_od_reading("REF", "90", interval=1, fake_data=True, experiment=experiment):
            pass
    assert "LED intensity" in str(error.value)

    with local_persistant_storage("current_od_calibration") as c:
        del c["90"]


def test_calibration_errors_when_pd_channel_differs():

    experiment = "test_calibration_errors_when_pd_channel_differs"

    with local_persistant_storage("current_od_calibration") as c:
        c["90"] = encode(
            structs.OD90Calibration(
                timestamp=current_utc_datetime(),
                curve_type="poly",
                curve_data_=[1.0, 0, -0.1],
                name="quad_test",
                maximum_od600=2.0,
                minimum_od600=0.0,
                ir_led_intensity=90.0,
                angle="90",
                minimum_voltage=0.0,
                maximum_voltage=1.0,
                voltages=[],
                inferred_od600s=[],
                pd_channel="2",
            )
        )

    with pytest.raises(exc.CalibrationError) as error:
        with start_od_reading("90", "REF", interval=1, fake_data=True, experiment=experiment):
            pass

    assert "channel" in str(error.value)

    with local_persistant_storage("current_od_calibration") as c:
        del c["90"]
