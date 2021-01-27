# -*- coding: utf-8 -*-
import socket
import time
import threading
import logging
from click import echo, style
from pioreactor.config import leader_hostname

# this can be improved by including job name, thread name, etc.


class QOS:
    AT_MOST_ONCE = 0
    AT_LEAST_ONCE = 1
    EXACTLY_ONCE = 2


def create_publishing_client(hostname=leader_hostname):
    import paho.mqtt.client as mqtt

    mqttc = mqtt.Client()
    mqttc.connect(hostname)
    mqttc.loop_start()
    return mqttc


def publish(topic, message, hostname=leader_hostname, retries=10, **mqtt_kwargs):
    from paho.mqtt import publish as mqtt_publish

    retry_count = 1
    while True:
        try:
            mqtt_publish.single(topic, payload=message, hostname=hostname, **mqtt_kwargs)
            return
        except (ConnectionRefusedError, socket.gaierror, OSError, socket.timeout) as e:
            # possible that leader is down/restarting, keep trying, but log to local machine.
            current_time = time.strftime("%Y-%m-%d %H:%M:%S")
            echo(
                style(f"{current_time}:", fg="white")
                + style(
                    f"Attempt {retry_count}: Unable to connect to host: {hostname}. {str(e)}",
                    fg="red",
                )
            )
            time.sleep(5 * retry_count)  # linear backoff
            retry_count += 1

        if retry_count == retries:
            raise ConnectionRefusedError(f"Unable to connect to host: {hostname}.")


def subscribe(topics, hostname=leader_hostname, retries=10, timeout=None, **mqtt_kwargs):
    """
    Modeled closely after the paho version, this also includes some try/excepts and
    a timeout. Note that this _does_ disconnect after receiving a single message.

    """
    import paho.mqtt.client as mqtt

    retry_count = 1
    while True:
        try:

            def on_connect(client, userdata, flags, rc):
                client.subscribe(userdata["topics"])
                return

            def on_message(client, userdata, message):
                userdata["messages"] = message
                client.disconnect()
                return

            topics = [topics] if isinstance(topics, str) else topics
            userdata = {
                "topics": [(topic, mqtt_kwargs.pop("qos", 0)) for topic in topics],
                "messages": None,
            }

            client = mqtt.Client(userdata=userdata)
            client.on_connect = on_connect
            client.on_message = on_message
            client.connect(leader_hostname)

            if timeout:
                threading.Timer(timeout, lambda: client.disconnect()).start()

            client.loop_forever()

            return userdata["messages"]

        except (ConnectionRefusedError, socket.gaierror, OSError, socket.timeout) as e:
            current_time = time.strftime("%Y-%m-%d %H:%M:%S")
            # possible that leader is down/restarting, keep trying, but log to local machine.
            echo(
                style(f"{current_time}:", fg="white")
                + style(
                    f"Attempt {retry_count}: Unable to connect to host: {hostname}. {str(e)}",
                    fg="red",
                )
            )
            time.sleep(5 * retry_count)  # linear backoff
            retry_count += 1

        if retry_count == retries:
            current_time = time.strftime("%Y-%m-%d %H:%M:%S")
            raise ConnectionRefusedError(f"Unable to connect to host: {hostname}.")


def subscribe_and_callback(
    callback,
    topics,
    hostname=leader_hostname,
    timeout=None,
    max_msgs=None,
    last_will=None,
    job_name=None,
    **mqtt_kwargs,
):
    """
    Creates a new thread, wrapping around paho's subscribe.callback. Callbacks only accept a single parameter, message.

    Parameters
    -------------
    timeout: float
        the client will  only listen for <timeout> seconds before disconnecting. (kinda)
    max_msgs: int
        the client will process <max_msgs> messages before disconnecting.
    last_will: dict
        a dictionary describing the last will details: topic, qos, retain, msg.
    job_name:
        Optional: provide the job name, and logging will include it.
    """
    import paho.mqtt.client as mqtt

    assert callable(
        callback
    ), "callback should be callable - do you need to change the order of arguments?"

    def on_connect(client, userdata, flags, rc):
        client.subscribe(userdata["topics"])

    def wrap_callback(actual_callback):
        def _callback(client, userdata, message):
            try:

                if "max_msgs" in userdata:
                    userdata["count"] += 1
                    if userdata["count"] > userdata["max_msgs"]:
                        client.loop_stop()
                        client.disconnect()
                        return

                return actual_callback(message)

            except Exception as e:
                logger = logging.getLogger(userdata.get("job_name", "pioreactor"))
                logger.error(e, exc_info=True)
                raise e

        return _callback

    topics = [topics] if isinstance(topics, str) else topics
    userdata = {
        "topics": [(topic, mqtt_kwargs.pop("qos", 0)) for topic in topics],
        "job_name": job_name,
    }

    if max_msgs:
        userdata["count"] = 0
        userdata["max_msgs"] = max_msgs

    client = mqtt.Client(userdata=userdata)
    client.on_connect = on_connect
    client.on_message = wrap_callback(callback)

    if last_will is not None:
        client.will_set(**last_will)

    client.connect(leader_hostname, **mqtt_kwargs)
    client.loop_start()

    def stop_and_disconnect():
        client.loop_stop()
        client.disconnect()

    if timeout:
        threading.Timer(timeout, lambda: stop_and_disconnect()).start()

    return client


def prune_retained_messages(topics_to_prune="#", hostname=leader_hostname):
    topics = []

    def on_message(message):
        topics.append(message.topic)

    client = subscribe_and_callback(
        on_message, topics_to_prune, hostname=hostname, timeout=1
    )

    for topic in topics.copy():
        publish(topic, None, retain=True, hostname=hostname)

    client.disconnect()
