# -*- coding: utf-8 -*-
import logging, uuid
from pioreactor.pubsub import create_client, publish
from pioreactor.whoami import (
    get_unit_name,
    am_I_active_worker,
    UNIVERSAL_EXPERIMENT,
    get_latest_experiment_name,
)
from pioreactor.config import config
import json_log_formatter

logging.raiseExceptions = False
# reduce logging from third party libs
logging.getLogger("sh").setLevel("ERROR")
logging.getLogger("paramiko").setLevel("ERROR")
logging.getLogger("sqlite3worker").setLevel("ERROR")


class CustomisedJSONFormatter(json_log_formatter.JSONFormatter):
    def json_record(self, message: str, extra: dict, record: logging.LogRecord) -> dict:
        extra["message"] = message

        # Include builtins
        extra["level"] = record.levelname
        extra["task"] = record.name

        if record.exc_info:
            extra["message"] += "\n" + self.formatException(record.exc_info)

        return extra


class MQTTHandler(logging.Handler):
    """
    A handler class which writes logging records, appropriately formatted,
    to a MQTT server to a topic.
    """

    def __init__(self, topic, client, qos=2, retain=False, **mqtt_kwargs):
        logging.Handler.__init__(self)
        self.topic = topic
        self.qos = qos
        self.retain = retain
        self.mqtt_kwargs = mqtt_kwargs
        self.client = client

    def emit(self, record):
        msg = self.format(record)
        self.client.publish(
            self.topic, msg, qos=self.qos, retain=self.retain, **self.mqtt_kwargs
        )

        if config.getboolean(
            "error_reporting", "send_to_Pioreactor_dot_com", fallback=False
        ):
            # turned off, by default
            if record.levelno == logging.ERROR:
                # TODO: build this service!
                publish(self.topic, msg, hostname="mqtt.pioreactor.com")


def create_logger(name, unit=None, experiment=None, pub_client=None, to_mqtt=True):
    """

    Parameters
    -----------
    name: string
        the name of the logger
    pub_client: paho.mqtt.Client
        use an existing Client, else one is created
    to_mqtt: bool
        connect and log to MQTT


    """

    logger = logging.getLogger(name)

    if len(logger.handlers) > 0:
        return logger

    logger.setLevel(logging.DEBUG)

    if unit is None:
        unit = get_unit_name()

    if experiment is None:
        experiment = get_latest_experiment_name()

    if (pub_client is None) and to_mqtt:
        pub_client = create_client(client_id=f"{unit}-logging-{uuid.uuid1()}")

    # file handler
    file_handler = logging.FileHandler(config["logging"]["log_file"])
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(
        logging.Formatter(
            "%(asctime)s [%(name)s] %(levelname)-2s %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )

    # define a Handler which writes INFO messages or higher to the sys.stderr
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.DEBUG)
    console_handler.setFormatter(
        logging.Formatter(
            "%(asctime)s [%(name)s] %(levelname)-2s %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )

    # add local log handlers
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)

    if to_mqtt:
        exp = experiment if am_I_active_worker() else UNIVERSAL_EXPERIMENT

        # create MQTT handlers for logs table
        topic = f"pioreactor/{unit}/{exp}/logs/app"
        mqtt_to_db_handler = MQTTHandler(topic, pub_client)
        mqtt_to_db_handler.setLevel(logging.DEBUG)
        mqtt_to_db_handler.setFormatter(CustomisedJSONFormatter())

        # add MQTT/remote log handlers
        logger.addHandler(mqtt_to_db_handler)

    return logger
