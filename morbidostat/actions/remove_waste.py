# remove waste
import time
import configparser
import click
import RPi.GPIO as GPIO
from  paho.mqtt import publish

config = configparser.ConfigParser()
config.read('config.ini')


@click.command()
@click.argument('ml', type=float)
def remove_waste(ml):
    GPIO.setmode(GPIO.BCM)

    WASTE_PIN = int(config['rpi_pins']['waste'])
    GPIO.setup(WASTE_PIN, GPIO.OUT)
    GPIO.output(WASTE_PIN, 1)

    # this should be a decorator at some point
    click.echo(click.style("starting remove_waste: %smL" % ml, fg='green'))

    GPIO.output(WASTE_PIN, 0)
    time.sleep(ml / float(config['pump_calibration']['waste_ml_per_second']))
    GPIO.output(WASTE_PIN, 1)

    publish.single("morbidostat/log", "remove_waste: %smL" % ml)
    publish.single("morbidostat/io_events", '{"volume_change": "-%s", "event": "remove_waste"}' % ml)
    click.echo(click.style("finished remove_waste: %smL" % ml, fg='green'))

    GPIO.cleanup()
    return

if __name__ == '__main__':
    try:
        remove_waste()
    except Exception as e:
        print(e)
        GPIO.cleanup()